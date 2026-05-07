from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
SECRETS_PATH = PROJECT_ROOT / ".secrets"
LOG_PATH = PROJECT_ROOT / "logs" / "llm-council.log"
MCP_HEALTH_PATH = PROJECT_ROOT / "runtime" / "mcp_health.json"
KEYRING_SERVICE = "llm-council-mcp"
KEYRING_USERNAME = "openrouter-api-key"

DEFAULT_COUNCIL_MODELS = [
    "openai/gpt-4.1-mini",
    "google/gemini-2.5-flash-preview",
]


@dataclass(slots=True)
class AppConfig:
    council_models: list[str]
    chairman_enabled: bool
    chairman_model: str | None
    council_timeout_ms: int
    frontend_port: int
    log_level: str


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _parse_models(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        models = [str(item).strip() for item in value if str(item).strip()]
        return models or None
    models = [item.strip() for item in str(value).split(",") if item.strip()]
    return models or None


def _load_json_config(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.json must contain a JSON object")
    return raw


def _legacy_secret_key(secret_path: Path = SECRETS_PATH) -> str | None:
    if not secret_path.exists():
        return None
    for line in secret_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == "OPENROUTER_API_KEY":
            secret = value.strip()
            return secret or None
    return None


def _keyring_client() -> tuple[Any, Any] | tuple[None, None]:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return None, None
    return keyring, KeyringError


def _load_keyring_secret() -> str | None:
    keyring_module, keyring_error = _keyring_client()
    if keyring_module is None:
        return None
    try:
        secret = keyring_module.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring_error:
        return None
    if not secret:
        return None
    normalized = secret.strip()
    return normalized or None


def _save_keyring_secret(api_key: str) -> None:
    keyring_module, keyring_error = _keyring_client()
    if keyring_module is None:
        raise RuntimeError(
            "A secure system keychain is required to store the OpenRouter API key. "
            "Install the project dependencies and configure a system keychain, or set OPENROUTER_API_KEY in the environment."
        )
    try:
        keyring_module.set_password(KEYRING_SERVICE, KEYRING_USERNAME, api_key)
    except keyring_error as exc:
        raise RuntimeError("Unable to store the OpenRouter API key in the system keychain.") from exc


def _delete_legacy_api_key(secret_path: Path = SECRETS_PATH) -> bool:
    if not secret_path.exists():
        return False
    existing_lines = secret_path.read_text(encoding="utf-8").splitlines()
    remaining_lines = [line for line in existing_lines if not line.strip().startswith("OPENROUTER_API_KEY=")]
    if len(remaining_lines) == len(existing_lines):
        return False
    if remaining_lines:
        secret_path.write_text("\n".join(remaining_lines) + "\n", encoding="utf-8")
    else:
        secret_path.unlink()
    return True


def _delete_keyring_secret() -> bool:
    keyring_module, keyring_error = _keyring_client()
    if keyring_module is None:
        return False
    existing_secret = _load_keyring_secret()
    if not existing_secret:
        return False
    try:
        keyring_module.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring_error:
        return False
    return True


def _load_stored_api_key(secret_path: Path = SECRETS_PATH) -> tuple[str | None, str]:
    keyring_secret = _load_keyring_secret()
    if keyring_secret:
        return keyring_secret, "keychain"

    legacy_secret = _legacy_secret_key(secret_path)
    if not legacy_secret:
        return None, "none"

    try:
        _save_keyring_secret(legacy_secret)
    except RuntimeError:
        return legacy_secret, "legacy-secrets"

    _delete_legacy_api_key(secret_path)
    return legacy_secret, "keychain"


def load_secret_key(secret_path: Path = SECRETS_PATH) -> str | None:
    stored_key, _ = _load_stored_api_key(secret_path)
    return stored_key


def mask_api_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}...{digest[-6:]}"


def get_api_key_status(environ: Mapping[str, str] | None = None, secret_path: Path = SECRETS_PATH) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    env_key = env.get("OPENROUTER_API_KEY")
    if env_key:
        return {
            "configured": True,
            "source": "environment",
            "preview": mask_api_key(env_key),
        }
    stored_key, source = _load_stored_api_key(secret_path)
    if stored_key:
        return {
            "configured": True,
            "source": source,
            "preview": mask_api_key(stored_key),
        }
    return {
        "configured": False,
        "source": "none",
        "preview": None,
    }


def validate_config(config: AppConfig) -> AppConfig:
    model_count = len(config.council_models)
    if model_count < 2 or model_count > 6:
        raise ValueError("Council must contain between 2 and 6 models.")
    if len(set(config.council_models)) != model_count:
        raise ValueError("Council model list must not contain duplicates.")
    if config.chairman_enabled and not config.chairman_model:
        raise ValueError("CHAIRMAN_MODEL is required when CHAIRMAN_ENABLED=true.")
    if config.council_timeout_ms < 10_000 or config.council_timeout_ms > 120_000:
        raise ValueError("COUNCIL_TIMEOUT_MS must be between 10000 and 120000.")
    return config


def load_config(environ: Mapping[str, str] | None = None, config_path: Path = CONFIG_PATH) -> AppConfig:
    env = os.environ if environ is None else environ
    file_config = _load_json_config(config_path)

    council_models = (
        _parse_models(env.get("COUNCIL_MODELS"))
        or _parse_models(file_config.get("council_models"))
        or DEFAULT_COUNCIL_MODELS.copy()
    )
    chairman_enabled = _parse_bool(env.get("CHAIRMAN_ENABLED"), _parse_bool(file_config.get("chairman_enabled"), False))
    chairman_model = env.get("CHAIRMAN_MODEL") or file_config.get("chairman_model")
    council_timeout_ms = int(env.get("COUNCIL_TIMEOUT_MS") or file_config.get("council_timeout_ms") or 60000)
    frontend_port = int(env.get("FRONTEND_PORT") or file_config.get("frontend_port") or 7842)
    log_level = str(env.get("LOG_LEVEL") or file_config.get("log_level") or "INFO").upper()

    return validate_config(
        AppConfig(
            council_models=council_models,
            chairman_enabled=chairman_enabled,
            chairman_model=str(chairman_model).strip() or None if chairman_model is not None else None,
            council_timeout_ms=council_timeout_ms,
            frontend_port=frontend_port,
            log_level=log_level,
        )
    )


def api_key_configured(environ: Mapping[str, str] | None = None, secret_path: Path = SECRETS_PATH) -> bool:
    return bool(get_api_key_status(environ=environ, secret_path=secret_path)["configured"])


def to_public_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "council_models": list(config.council_models),
        "chairman_enabled": config.chairman_enabled,
        "chairman_model": config.chairman_model,
        "council_timeout_ms": config.council_timeout_ms,
        "frontend_port": config.frontend_port,
        "log_level": config.log_level,
    }


def save_config(config: AppConfig, config_path: Path = CONFIG_PATH) -> None:
    payload = asdict(validate_config(config))
    config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def save_api_key(api_key: str, secret_path: Path = SECRETS_PATH) -> None:
    normalized_key = api_key.strip()
    if not normalized_key:
        raise ValueError("OpenRouter API key cannot be empty.")
    _save_keyring_secret(normalized_key)
    _delete_legacy_api_key(secret_path)


def delete_api_key(secret_path: Path = SECRETS_PATH) -> bool:
    removed_keyring = _delete_keyring_secret()
    removed_legacy = _delete_legacy_api_key(secret_path)
    return removed_keyring or removed_legacy

