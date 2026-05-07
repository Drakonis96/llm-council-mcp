from __future__ import annotations

import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator

from config import LOG_PATH, MCP_HEALTH_PATH, SECRETS_PATH, load_secret_key

LOGGER_NAME = "llm_council"
API_KEY_PATTERN = re.compile(r"sk-or-v1-[A-Za-z0-9_-]+")
_API_KEY: str | None = None
_API_KEY_OVERRIDE: ContextVar[str | None] = ContextVar("openrouter_api_key_override", default=None)


def load_api_key(environ: dict[str, str] | None = None, secret_path: Path = SECRETS_PATH) -> str:
    env = environ if environ is not None else os.environ
    api_key = env.get("OPENROUTER_API_KEY") or load_secret_key(secret_path)
    if api_key:
        return api_key
    raise SystemExit(
        "OPENROUTER_API_KEY not found. Set it as an environment variable or store it through the dashboard system keychain flow."
    )


def initialize_api_key(environ: dict[str, str] | None = None, secret_path: Path = SECRETS_PATH, force_reload: bool = False) -> str:
    global _API_KEY
    if _API_KEY and not force_reload:
        return _API_KEY
    _API_KEY = load_api_key(environ=environ, secret_path=secret_path)
    return _API_KEY


def reset_api_key() -> None:
    global _API_KEY
    _API_KEY = None


def get_api_key() -> str:
    override = _API_KEY_OVERRIDE.get()
    if override:
        return override
    if _API_KEY:
        return _API_KEY
    raise RuntimeError("OpenRouter API key has not been initialized.")


@contextmanager
def temporary_api_key(api_key: str | None) -> Iterator[None]:
    if not api_key:
        yield
        return
    token = _API_KEY_OVERRIDE.set(api_key)
    try:
        yield
    finally:
        _API_KEY_OVERRIDE.reset(token)


def sanitize_log_text(message: str) -> str:
    return API_KEY_PATTERN.sub("[REDACTED]", message)


class SecretSanitizingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original_message = record.getMessage()
        sanitized = sanitize_log_text(original_message)
        clone = logging.makeLogRecord({**record.__dict__, "msg": sanitized, "args": ()})
        return super().format(clone)


def configure_logging(log_level: str = "INFO", enable_stderr: bool = True) -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.propagate = False
    for handler in logger.handlers:
        handler.close()
    formatter = SecretSanitizingFormatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = []

    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    handlers.append(file_handler)

    if enable_stderr:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        handlers.append(stderr_handler)

    logger.handlers = handlers
    return logger


def _build_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost",
        "X-OpenRouter-Title": "llm-council-mcp",
    }


def write_mcp_health(status: str, pid: int) -> None:
    MCP_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "pid": pid}
    MCP_HEALTH_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_mcp_health() -> dict[str, object]:
    if not MCP_HEALTH_PATH.exists():
        return {"status": "unknown", "pid": None}
    try:
        payload = json.loads(MCP_HEALTH_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "unknown", "pid": None}
    if not isinstance(payload, dict):
        return {"status": "unknown", "pid": None}
    return payload


def tail_sanitized_log_lines(limit: int = 50) -> list[str]:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    return [sanitize_log_text(line) for line in lines[-limit:]]

