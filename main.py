from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from config import CONFIG_PATH, AppConfig, SECRETS_PATH, load_config
from council import CouncilService
from security import configure_logging, initialize_api_key, write_mcp_health


@dataclass(slots=True)
class McpRuntime:
    config: AppConfig
    service: CouncilService


def bootstrap_runtime(
    environ: Mapping[str, str] | None = None,
    config_path: Path = CONFIG_PATH,
    secret_path: Path = SECRETS_PATH,
) -> McpRuntime:
    config = load_config(environ=environ, config_path=config_path)
    initialize_api_key(environ=dict(environ) if environ is not None else None, secret_path=secret_path, force_reload=True)
    logger = configure_logging(config.log_level)
    service = CouncilService(config=config, logger=logger)
    return McpRuntime(config=config, service=service)


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[McpRuntime]:
    runtime = bootstrap_runtime()
    write_mcp_health("running", os.getpid())
    try:
        yield runtime
    finally:
        write_mcp_health("stopped", os.getpid())


mcp = FastMCP(
    name="llm-council",
    instructions="Consult multiple anonymized OpenRouter-backed models and optionally synthesize them with a chairman model.",
    lifespan=app_lifespan,
)


@mcp.tool()
async def council_consult(
    question: str,
    ctx: Context[ServerSession, McpRuntime],
    context: str | None = None,
) -> str:
    """Consult the configured council of LLMs with an optional context block."""
    runtime = ctx.request_context.lifespan_context
    return await runtime.service.consult(question=question, context=context)


@mcp.tool()
async def council_status(ctx: Context[ServerSession, McpRuntime]) -> dict[str, object]:
    """Return the currently active public council configuration."""
    runtime = ctx.request_context.lifespan_context
    return runtime.service.status()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
