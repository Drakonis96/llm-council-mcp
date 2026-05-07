from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import CONFIG_PATH, AppConfig, api_key_configured, delete_api_key, get_api_key_status, load_config, save_api_key, save_config, to_public_dict
from council import OpenRouterTransport
from security import (
    configure_logging,
    initialize_api_key,
    read_mcp_health,
    reset_api_key,
    tail_sanitized_log_lines,
    temporary_api_key,
)

PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@dataclass(slots=True)
class WebRuntime:
    config: AppConfig
    transport: OpenRouterTransport


class ConnectionTestRequest(BaseModel):
    api_key: str | None = Field(default=None)


class ConfigSaveRequest(BaseModel):
    api_key: str | None = Field(default=None)
    council_models: list[str]
    chairman_enabled: bool = False
    chairman_model: str | None = None
    council_timeout_ms: int = 60_000


def _api_key_payload() -> dict[str, Any]:
    return get_api_key_status()


def _mcp_client_payload() -> dict[str, Any]:
    command = shutil.which("uv") or "uv"
    project_root = str(PROJECT_ROOT)
    snippet = {
        "mcpServers": {
            "llm-council": {
                "command": command,
                "args": ["--directory", project_root, "run", "python", "main.py"],
                "env": {
                    "OPENROUTER_API_KEY": "sk-or-v1-your-key-here",
                },
            }
        }
    }
    return {
        "command": command,
        "project_root": project_root,
        "snippet": json.dumps(snippet, indent=2),
    }


def _control_mcp_process(action: str) -> dict[str, Any]:
    if shutil.which("pm2") is None:
        return {"attempted": False, "succeeded": False, "message": "pm2 is not installed."}
    result = subprocess.run(
        ["pm2", action, "llm-council-mcp"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "attempted": True,
        "succeeded": result.returncode == 0,
        "message": result.stdout.strip() or result.stderr.strip() or "Restart command issued.",
    }


def _restart_mcp_process() -> dict[str, Any]:
    return _control_mcp_process("restart")


def _stop_mcp_process() -> dict[str, Any]:
    return _control_mcp_process("stop")


def _mcp_health_payload() -> dict[str, Any]:
    payload = read_mcp_health()
    pid = payload.get("pid")
    running = False
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            running = True
        except ProcessLookupError:
            running = False
        except PermissionError:
            running = True
    return {
        "status": "running" if running else "stopped",
        "pid": pid,
        "reported_status": payload.get("status", "unknown"),
    }


@asynccontextmanager
async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    configure_logging(config.log_level)
    app.state.runtime = WebRuntime(config=config, transport=OpenRouterTransport())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="llm-council-mcp dashboard", lifespan=app_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        runtime: WebRuntime = app.state.runtime
        api_key = _api_key_payload()
        return {
            "config": to_public_dict(runtime.config),
            "api_key": api_key,
            "mcp_client": _mcp_client_payload(),
            "api_key_configured": api_key["configured"],
            "mcp_health": _mcp_health_payload(),
        }

    @app.get("/api/health")
    async def get_health() -> dict[str, Any]:
        return _mcp_health_payload()

    @app.get("/api/logs")
    async def get_logs(limit: int = 50) -> dict[str, Any]:
        safe_limit = max(1, min(limit, 200))
        return {"lines": tail_sanitized_log_lines(safe_limit)}

    @app.post("/api/test-connection")
    async def test_connection(request: ConnectionTestRequest) -> dict[str, Any]:
        runtime: WebRuntime = app.state.runtime
        submitted_key = request.api_key.strip() if request.api_key else None
        try:
            if not submitted_key:
                initialize_api_key(force_reload=True)
            with temporary_api_key(submitted_key):
                key_info = await runtime.transport.get_key_info()
                models = await runtime.transport.list_models()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=400, detail=f"OpenRouter rejected the connection test with status {exc.response.status_code}.") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Unable to reach OpenRouter: {type(exc).__name__}.") from exc
        return {
            "ok": True,
            "model_count": len(models),
            "models": models,
            "usage": {
                "limit_remaining": key_info.get("limit_remaining"),
                "is_free_tier": key_info.get("is_free_tier"),
            },
        }

    @app.post("/api/config")
    async def persist_config(request: ConfigSaveRequest) -> dict[str, Any]:
        runtime: WebRuntime = app.state.runtime
        new_config = AppConfig(
            council_models=request.council_models,
            chairman_enabled=request.chairman_enabled,
            chairman_model=request.chairman_model,
            council_timeout_ms=request.council_timeout_ms,
            frontend_port=runtime.config.frontend_port,
            log_level=runtime.config.log_level,
        )
        try:
            save_config(new_config, CONFIG_PATH)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if request.api_key and request.api_key.strip():
            try:
                save_api_key(request.api_key.strip())
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            initialize_api_key(force_reload=True)
        runtime.config = new_config
        configure_logging(new_config.log_level)
        api_key = _api_key_payload()
        return {
            "ok": True,
            "config": to_public_dict(new_config),
            "api_key": api_key,
            "api_key_configured": api_key["configured"],
            "restart": _restart_mcp_process(),
        }

    @app.delete("/api/config/api-key")
    async def remove_api_key() -> dict[str, Any]:
        removed = delete_api_key()
        status = _api_key_payload()
        if status["configured"]:
            initialize_api_key(force_reload=True)
        else:
            reset_api_key()
        status = _api_key_payload()
        if removed:
            message = "Stored API key removed."
        elif status["source"] == "environment":
            message = "API key is provided by environment variables and cannot be removed from the dashboard."
        else:
            message = "No stored API key found."
        return {
            "ok": True,
            "removed": removed,
            "message": message,
            "api_key": status,
            "api_key_configured": status["configured"],
        }

    @app.post("/api/server/stop")
    async def stop_server() -> dict[str, Any]:
        return {
            "ok": True,
            "stop": _stop_mcp_process(),
            "mcp_health": _mcp_health_payload(),
        }

    if FRONTEND_DIST.exists():
        assets_dir = FRONTEND_DIST / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/", include_in_schema=False)
        async def serve_root() -> FileResponse:
            return FileResponse(FRONTEND_DIST / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str):
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            file_path = FRONTEND_DIST / full_path
            if file_path.exists() and file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(FRONTEND_DIST / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def frontend_not_built() -> PlainTextResponse:
            return PlainTextResponse(
                "Frontend build not found. Run npm install && npm run build in frontend/ before starting the dashboard.",
                status_code=503,
            )

    return app


app = create_app()


def main() -> None:
    config = load_config()
    uvicorn.run("web_app:app", host="0.0.0.0", port=config.frontend_port, reload=False)


if __name__ == "__main__":
    main()
