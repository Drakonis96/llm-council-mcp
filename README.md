# llm-council-mcp

`llm-council-mcp` is a production-oriented local MCP server that queries multiple OpenRouter models in parallel, anonymizes them as `Model A`, `Model B`, and so on, and optionally asks a chairman model to synthesize the responses.

The project ships with:

- A stdio MCP server for LLM clients
- A FastAPI control plane for configuration, health, connection testing, and log viewing
- A React dashboard built with Vite
- PM2 startup automation through `setup.sh`

## Architecture

- `main.py`: stdio MCP server exposing `council_consult` and `council_status`
- `council.py`: parallel OpenRouter calls, stable anonymization, chairman synthesis
- `config.py`: public config loading plus system-keychain secret storage and legacy `.secrets` migration
- `security.py`: API key lifecycle, header construction, log sanitization, health and log helpers
- `web_app.py`: FastAPI dashboard backend and static frontend host
- `frontend/`: React + Vite dashboard source

The MCP server and the dashboard are separate processes. In production, `setup.sh` registers both with PM2.

## Security Model

The OpenRouter API key is treated as a runtime secret, not application configuration.

- Secrets are accepted from `OPENROUTER_API_KEY`, the system keychain, or a legacy `.secrets` entry during migration
- Keys saved from the dashboard are written to the system keychain, not to plaintext project files
- Legacy `.secrets` entries are migrated into the keychain on first successful read
- `config.json` never stores secrets and is safe to commit
- The runtime key is loaded once into a module-level slot for the MCP process
- The dashboard and API expose only a SHA-256 fingerprint, never a reversible preview of the key
- All OpenRouter headers are built only inside `security._build_headers()`
- Logs are sanitized to redact anything matching `sk-or-v1-...`
- Council output never exposes real model slugs; responses are always labeled `Model A`, `Model B`, and so on
- Timeout and failure messages use anonymized labels only

If the MCP server starts without a configured key, it exits with:

```text
OPENROUTER_API_KEY not found. Set it as an environment variable or store it through the dashboard system keychain flow.
```

## Configuration Priority

Public config values resolve in this order:

1. Environment variables
2. `config.json`
3. Built-in defaults

OpenRouter API key resolution is separate:

1. `OPENROUTER_API_KEY` environment variable
2. System keychain entry created by the dashboard
3. Legacy `.secrets` file, which is migrated into the keychain when possible

Supported environment variables:

- `OPENROUTER_API_KEY`
- `COUNCIL_MODELS`
- `CHAIRMAN_ENABLED`
- `CHAIRMAN_MODEL`
- `COUNCIL_TIMEOUT_MS`
- `FRONTEND_PORT`
- `LOG_LEVEL`

## Quick Start

1. Install dependencies:

```bash
./setup.sh
```

2. Configure the OpenRouter API key:

- Recommended for local use: open the dashboard and save the key once; it will be stored in the system keychain
- Alternative: export `OPENROUTER_API_KEY` in your shell or define it in your MCP client config
- Legacy `.secrets` files are still read for migration, but they are no longer the recommended storage path

3. Start the dashboard if you are developing locally:

```bash
uv run python web_app.py
```

4. Start the MCP server if you want to run it manually:

```bash
uv run python main.py
```

## Example Configuration File

Use the following as a reference for your environment configuration. The repository already includes `.env.example`, but this example explains the expected format.

```dotenv
# API key: use this only if you inject env vars from your shell, PM2, or another process manager.
OPENROUTER_API_KEY=sk-or-v1-your-key-here

# Council models are comma-separated, with no quotes.
# Each entry must be a full OpenRouter model slug.
COUNCIL_MODELS=openai/gpt-4.1-mini,google/gemini-2.5-flash-preview,anthropic/claude-3.7-sonnet

# Enable chairman synthesis.
CHAIRMAN_ENABLED=false

# Required only when CHAIRMAN_ENABLED=true.
CHAIRMAN_MODEL=openai/gpt-4.1-mini

# Timeout per council model request, in milliseconds.
COUNCIL_TIMEOUT_MS=60000

# Dashboard port.
FRONTEND_PORT=7842

# One of: DEBUG, INFO, WARNING, ERROR.
LOG_LEVEL=INFO
```

Notes:

- `COUNCIL_MODELS` must contain between 2 and 6 models.
- Separate models with commas: `model-a,model-b,model-c`
- Do not wrap model slugs in quotes.
- If `CHAIRMAN_ENABLED=true`, then `CHAIRMAN_MODEL` must be set.
- The app does not automatically load a `.env` file by itself. Use real environment variables for runtime config.
- The dashboard keychain entry is local to the dashboard process. External MCP clients still need `OPENROUTER_API_KEY` in their own config or shell environment.

## Setup

### Requirements

- `uv`
- `npm`
- `pm2`
- Python 3.11+

### Automated setup

```bash
./setup.sh
```

`setup.sh` does the following:

1. Installs Python dependencies with `uv sync --all-extras`
2. Installs frontend dependencies with `npm install`
3. Builds the frontend with `npm run build`
4. Writes `.gitignore` with the required secret and runtime entries
5. Registers the MCP server and dashboard with PM2
6. Saves the PM2 process list and attempts startup registration

If `pm2 startup` requires a privileged follow-up command on your machine, PM2 will print it.

## Development

### Python

```bash
uv sync --all-extras
uv run pytest tests/test_security.py
```

### Dashboard frontend

```bash
cd frontend
npm install
npm run dev
```

### Dashboard backend

```bash
uv run python web_app.py
```

### MCP server

```bash
export OPENROUTER_API_KEY=your-key-here
uv run python main.py
```

## MCP Registration

Use this single universal `mcpServers` block for Claude Desktop, VS Code MCP, Cherry Studio, Cursor, and similar stdio clients.

The dashboard renders the same block with the project path auto-detected from the running backend on your machine. In the README, the path stays generic on purpose.

Dashboard note:
The dashboard stores its own key in the system keychain, but Claude Desktop, Cherry Studio, Cursor, VS Code, and similar MCP apps still need `OPENROUTER_API_KEY` inside their own process environment.

Paste into your MCP client config:

```json
{
	"mcpServers": {
		"llm-council": {
			"command": "uv",
			"args": [
				"--directory",
				"/absolute/path/to/llm-council-mcp",
				"run",
				"python",
				"main.py"
			],
			"env": {
				"OPENROUTER_API_KEY": "sk-or-v1-your-key-here"
			}
		}
	}
}
```

If your client asks for a single server object instead of an `mcpServers` map, copy only the value inside `llm-council`.

## Tool Behavior

### `council_consult`

- Input: `question` and optional `context`
- Sends the request to every configured council model concurrently
- Returns anonymized sections if the chairman is disabled
- Returns a chairman synthesis if the chairman is enabled

When the chairman is enabled, the following hardcoded system prompt is used:

```text
You are synthesizing responses from multiple AI models to a complex question.
You do not know which model produced which response.
Identify where models agree, where they diverge, and why.
Produce a single coherent response that captures the strongest insights 
from all positions.
Be explicit about significant disagreements rather than papering over them.
Do not speculate about model identities.
```

### `council_status`

Returns:

- Number of active council models
- Whether the chairman is enabled
- The chairman model slug if enabled
- Configured timeout

The tool does not expose API keys or the internal model-to-label mapping.

## Dashboard Features

The dashboard runs on `FRONTEND_PORT` with a default of `7842`.

It provides:

- Secure API key entry stored in the system keychain with SHA-256 fingerprint display
- OpenRouter connection testing and model discovery
- Searchable model selection for 2 to 6 council members
- Optional chairman model selection
- Timeout control from 10 to 120 seconds
- Save and MCP restart action
- MCP health reporting
- Sanitized live logs showing the last 50 lines
- Copy-paste MCP client snippets for Claude Desktop, Cherry Studio, and VS Code MCP

## Usage Examples

### Example 1: Council members only

Question:

```text
Should we migrate this service to an event-driven architecture?
```

Response shape:

```text
Model A
...response...

Model B
...response...

Model C
...response...
```

### Example 2: Chairman enabled

Question:

```text
What is the safest rollout strategy for a breaking API change?
```

Response shape:

```text
The council broadly agrees that ...
```

## Tests

The required security suite lives in `tests/test_security.py` and verifies:

- API keys never appear in logs
- API keys never appear in MCP responses
- `config.json` never stores secrets
- Real model slugs never appear in council output
- Anonymization stays stable within a process session
- Missing API keys fail startup cleanly
- Timeout messages never expose real model identity
