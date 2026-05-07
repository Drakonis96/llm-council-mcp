from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from config import AppConfig
from security import _build_headers

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CHAIRMAN_SYSTEM_PROMPT = (
    "You are synthesizing responses from multiple AI models to a complex question.\n"
    "You do not know which model produced which response.\n"
    "Identify where models agree, where they diverge, and why.\n"
    "Produce a single coherent response that captures the strongest insights \n"
    "from all positions.\n"
    "Be explicit about significant disagreements rather than papering over them.\n"
    "Do not speculate about model identities."
)


class CouncilTransport(Protocol):
    async def chat_completion(self, model: str, messages: list[dict[str, str]], timeout_ms: int) -> str:
        ...

    async def list_models(self) -> list[dict[str, Any]]:
        ...

    async def get_key_info(self) -> dict[str, Any]:
        ...


class OpenRouterTransport:
    async def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None, timeout_s: float = 30.0) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=OPENROUTER_BASE_URL, headers=_build_headers()) as client:
            response = await client.request(method, path, json=json_body, timeout=timeout_s)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("OpenRouter returned an unexpected response format.")
            return payload

    async def chat_completion(self, model: str, messages: list[dict[str, str]], timeout_ms: int) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        response = await self._request("POST", "/chat/completions", json_body=payload, timeout_s=timeout_ms / 1000)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenRouter returned no completion choices.")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise RuntimeError("OpenRouter returned an invalid completion choice.")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("OpenRouter returned an invalid completion message.")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        raise RuntimeError("OpenRouter returned an empty completion.")

    async def list_models(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/models?output_modalities=text", timeout_s=30.0)
        rows = response.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("OpenRouter models endpoint returned an invalid payload.")
        results: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            results.append(
                {
                    "id": row.get("id"),
                    "name": row.get("name") or row.get("id"),
                    "description": row.get("description") or "",
                    "context_length": row.get("context_length"),
                    "canonical_slug": row.get("canonical_slug") or row.get("id"),
                    "supported_parameters": row.get("supported_parameters") or [],
                }
            )
        return sorted(results, key=lambda item: str(item.get("name", "")).lower())

    async def get_key_info(self) -> dict[str, Any]:
        response = await self._request("GET", "/key", timeout_s=20.0)
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("OpenRouter key endpoint returned an invalid payload.")
        return data


@dataclass(slots=True)
class CouncilSlotResult:
    label: str
    model_slug: str
    ok: bool
    content: str
    latency_ms: int

    def rendered(self) -> str:
        return self.content.strip()


class CouncilService:
    def __init__(self, config: AppConfig, logger: logging.Logger, transport: CouncilTransport | None = None) -> None:
        self.logger = logger
        self.transport = transport or OpenRouterTransport()
        self.refresh_config(config)

    def refresh_config(self, config: AppConfig) -> None:
        self.config = config
        self._label_by_model = {
            model_slug: f"Model {chr(65 + index)}"
            for index, model_slug in enumerate(config.council_models)
        }

    def label_for_model(self, model_slug: str) -> str:
        return self._label_by_model[model_slug]

    async def consult(self, question: str, context: str | None = None) -> str:
        start = time.perf_counter()
        self.logger.info(
            "tool_call name=council_consult model_count=%s chairman_enabled=%s",
            len(self.config.council_models),
            self.config.chairman_enabled,
        )
        messages = self._build_user_messages(question, context)
        results = await asyncio.gather(
            *(self._query_council_model(model_slug, messages) for model_slug in self.config.council_models)
        )

        if self.config.chairman_enabled and self.config.chairman_model:
            output = await self._synthesize_with_chairman(question, context, results)
        else:
            output = self._render_individual_responses(results)

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        self.logger.info("tool_complete name=council_consult latency_ms=%s", elapsed_ms)
        return output

    def status(self) -> dict[str, Any]:
        self.logger.info(
            "tool_call name=council_status model_count=%s chairman_enabled=%s",
            len(self.config.council_models),
            self.config.chairman_enabled,
        )
        return {
            "active_model_count": len(self.config.council_models),
            "chairman_enabled": self.config.chairman_enabled,
            "chairman_model": self.config.chairman_model if self.config.chairman_enabled else None,
            "council_timeout_ms": self.config.council_timeout_ms,
        }

    async def _query_council_model(self, model_slug: str, messages: list[dict[str, str]]) -> CouncilSlotResult:
        label = self.label_for_model(model_slug)
        start = time.perf_counter()
        try:
            content = await self.transport.chat_completion(model_slug, messages, self.config.council_timeout_ms)
            latency_ms = int((time.perf_counter() - start) * 1000)
            self.logger.info("slot_result slot=%s status=success latency_ms=%s", label, latency_ms)
            return CouncilSlotResult(label=label, model_slug=model_slug, ok=True, content=content, latency_ms=latency_ms)
        except httpx.TimeoutException:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self.logger.warning("slot_result slot=%s status=timeout latency_ms=%s", label, latency_ms)
            return CouncilSlotResult(
                label=label,
                model_slug=model_slug,
                ok=False,
                content=f"{label} timed out after {self.config.council_timeout_ms / 1000:.1f}s.",
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self.logger.warning(
                "slot_result slot=%s status=failure latency_ms=%s error=%s",
                label,
                latency_ms,
                type(exc).__name__,
            )
            return CouncilSlotResult(
                label=label,
                model_slug=model_slug,
                ok=False,
                content=f"{label} failed to respond.",
                latency_ms=latency_ms,
            )

    def _build_user_messages(self, question: str, context: str | None) -> list[dict[str, str]]:
        prompt = question.strip()
        if context:
            prompt = f"Context:\n{context.strip()}\n\nQuestion:\n{question.strip()}"
        return [{"role": "user", "content": prompt}]

    async def _synthesize_with_chairman(self, question: str, context: str | None, results: list[CouncilSlotResult]) -> str:
        chairman_messages = [
            {"role": "system", "content": CHAIRMAN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._build_chairman_prompt(question, context, results),
            },
        ]
        start = time.perf_counter()
        try:
            synthesis = await self.transport.chat_completion(
                self.config.chairman_model or "",
                chairman_messages,
                self.config.council_timeout_ms,
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
            self.logger.info("slot_result slot=Chairman status=success latency_ms=%s", latency_ms)
            return synthesis
        except httpx.TimeoutException:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self.logger.warning("slot_result slot=Chairman status=timeout latency_ms=%s", latency_ms)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self.logger.warning(
                "slot_result slot=Chairman status=failure latency_ms=%s error=%s",
                latency_ms,
                type(exc).__name__,
            )
        return (
            "Chairman synthesis was unavailable, so the council is returning the anonymized member responses instead.\n\n"
            + self._render_individual_responses(results)
        )

    def _build_chairman_prompt(self, question: str, context: str | None, results: list[CouncilSlotResult]) -> str:
        sections = "\n\n".join(f"{result.label}:\n{result.rendered()}" for result in results)
        context_block = context.strip() if context else "No additional context provided."
        return f"Question:\n{question.strip()}\n\nContext:\n{context_block}\n\nCouncil Responses:\n{sections}"

    def _render_individual_responses(self, results: list[CouncilSlotResult]) -> str:
        blocks = [f"{result.label}\n{result.rendered()}" for result in results]
        return "\n\n".join(blocks)
