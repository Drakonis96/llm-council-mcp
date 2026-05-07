from __future__ import annotations

import json

import httpx
import pytest

import config as config_module
from config import AppConfig, get_api_key_status, load_config, load_secret_key, save_api_key, save_config
from council import CouncilService
from main import bootstrap_runtime
from security import configure_logging, temporary_api_key


class FakeTransport:
    def __init__(self, responses: dict[str, str | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def chat_completion(self, model: str, messages: list[dict[str, str]], timeout_ms: int) -> str:
        self.calls.append(model)
        outcome = self.responses[model]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def list_models(self) -> list[dict[str, object]]:
        return []

    async def get_key_info(self) -> dict[str, object]:
        return {}


def build_config() -> AppConfig:
    return AppConfig(
        council_models=["deepseek/deepseek-v4-flash", "openai/gpt-4.1-mini"],
        chairman_enabled=False,
        chairman_model=None,
        council_timeout_ms=60_000,
        frontend_port=7842,
        log_level="INFO",
    )


def test_api_key_not_in_config_json(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    secret_path = tmp_path / ".secrets"
    stored_secret: dict[str, str] = {}

    def fake_save_keyring_secret(api_key: str) -> None:
        stored_secret["value"] = api_key

    def fake_load_keyring_secret() -> str | None:
        return stored_secret.get("value")

    config = build_config()

    monkeypatch.setattr(config_module, "_save_keyring_secret", fake_save_keyring_secret)
    monkeypatch.setattr(config_module, "_load_keyring_secret", fake_load_keyring_secret)
    save_api_key("sk-or-v1-top-secret", secret_path)
    save_config(config, config_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert "OPENROUTER_API_KEY" not in payload
    assert "sk-or-v1-top-secret" not in config_path.read_text(encoding="utf-8")
    assert stored_secret["value"] == "sk-or-v1-top-secret"
    assert not secret_path.exists()


def test_api_key_status_uses_fingerprint_and_keychain_source(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config_module, "_load_keyring_secret", lambda: "sk-or-v1-top-secret")

    status = get_api_key_status(environ={}, secret_path=tmp_path / ".secrets")

    assert status["configured"] is True
    assert status["source"] == "keychain"
    assert status["preview"].startswith("sha256:")
    assert "top-secret" not in status["preview"]


def test_legacy_secret_is_migrated_out_of_plaintext_file(monkeypatch, tmp_path) -> None:
    secret_path = tmp_path / ".secrets"
    secret_path.write_text("OPENROUTER_API_KEY=sk-or-v1-top-secret\n", encoding="utf-8")
    stored_secret: dict[str, str] = {}

    monkeypatch.setattr(config_module, "_load_keyring_secret", lambda: stored_secret.get("value"))
    monkeypatch.setattr(config_module, "_save_keyring_secret", lambda api_key: stored_secret.__setitem__("value", api_key))

    loaded_key = load_secret_key(secret_path)

    assert loaded_key == "sk-or-v1-top-secret"
    assert stored_secret["value"] == "sk-or-v1-top-secret"
    assert not secret_path.exists()


@pytest.mark.anyio
async def test_api_key_not_in_logs(capsys) -> None:
    logger = configure_logging("INFO")
    service = CouncilService(
        config=build_config(),
        logger=logger,
        transport=FakeTransport(
            {
                "deepseek/deepseek-v4-flash": "Answer from first model.",
                "openai/gpt-4.1-mini": "Answer from second model.",
            }
        ),
    )

    with temporary_api_key("sk-or-v1-top-secret"):
        await service.consult("What should the council say?")
        logger.info("diagnostic key sk-or-v1-top-secret")

    stderr = capsys.readouterr().err
    assert "sk-or-v1-top-secret" not in stderr
    assert "[REDACTED]" in stderr


@pytest.mark.anyio
async def test_api_key_not_in_mcp_response() -> None:
    logger = configure_logging("INFO")
    service = CouncilService(
        config=build_config(),
        logger=logger,
        transport=FakeTransport(
            {
                "deepseek/deepseek-v4-flash": "Option one.",
                "openai/gpt-4.1-mini": "Option two.",
            }
        ),
    )

    with temporary_api_key("sk-or-v1-top-secret"):
        status_payload = json.dumps(service.status())
        consult_payload = await service.consult("Summarize the options.")

    assert "sk-or-v1-top-secret" not in status_payload
    assert "sk-or-v1-top-secret" not in consult_payload


@pytest.mark.anyio
async def test_model_names_not_in_response() -> None:
    logger = configure_logging("INFO")
    transport = FakeTransport(
        {
            "deepseek/deepseek-v4-flash": "Use a layered approach.",
            "openai/gpt-4.1-mini": "Favor the lowest-risk option.",
        }
    )
    service = CouncilService(config=build_config(), logger=logger, transport=transport)

    result = await service.consult("How should we proceed?")

    assert "Model A" in result
    assert "Model B" in result
    assert "deepseek/deepseek-v4-flash" not in result
    assert "openai/gpt-4.1-mini" not in result


@pytest.mark.anyio
async def test_anonymization_is_stable() -> None:
    logger = configure_logging("INFO")
    transport = FakeTransport(
        {
            "deepseek/deepseek-v4-flash": "First response.",
            "openai/gpt-4.1-mini": "Second response.",
        }
    )
    service = CouncilService(config=build_config(), logger=logger, transport=transport)

    first = await service.consult("Question one?")
    second = await service.consult("Question two?")

    assert service.label_for_model("deepseek/deepseek-v4-flash") == "Model A"
    assert service.label_for_model("openai/gpt-4.1-mini") == "Model B"
    assert first.index("Model A") < first.index("Model B")
    assert second.index("Model A") < second.index("Model B")


def test_env_overrides_config(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "council_models": ["file-model-a", "file-model-b"],
                "chairman_enabled": False,
                "chairman_model": None,
                "council_timeout_ms": 30000,
                "frontend_port": 7000,
                "log_level": "warning",
            }
        ),
        encoding="utf-8",
    )

    config = load_config(
        environ={
            "COUNCIL_MODELS": "env-model-a,env-model-b",
            "CHAIRMAN_ENABLED": "true",
            "CHAIRMAN_MODEL": "env-chairman",
            "COUNCIL_TIMEOUT_MS": "90000",
            "FRONTEND_PORT": "7842",
            "LOG_LEVEL": "debug",
        },
        config_path=config_path,
    )

    assert config.council_models == ["env-model-a", "env-model-b"]
    assert config.chairman_enabled is True
    assert config.chairman_model == "env-chairman"
    assert config.council_timeout_ms == 90000
    assert config.frontend_port == 7842
    assert config.log_level == "DEBUG"


def test_missing_api_key_exits_cleanly(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    save_config(build_config(), config_path)
    monkeypatch.setattr(config_module, "_load_keyring_secret", lambda: None)

    with pytest.raises(SystemExit, match="dashboard system keychain flow"):
        bootstrap_runtime(environ={}, config_path=config_path, secret_path=tmp_path / ".secrets")


@pytest.mark.anyio
async def test_timeout_does_not_expose_model_identity() -> None:
    logger = configure_logging("INFO")
    service = CouncilService(
        config=build_config(),
        logger=logger,
        transport=FakeTransport(
            {
                "deepseek/deepseek-v4-flash": httpx.TimeoutException("timed out deepseek/deepseek-v4-flash"),
                "openai/gpt-4.1-mini": "Second model still replied.",
            }
        ),
    )

    result = await service.consult("What happened?")

    assert "Model A timed out" in result
    assert "deepseek/deepseek-v4-flash" not in result