#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Missing required command: $name" >&2
    exit 1
  fi
}

require_command uv
require_command npm
require_command pm2

cat > "$PROJECT_ROOT/.gitignore" <<'EOF'
.secrets
.env
__pycache__/
.venv/
.pytest_cache/
frontend/node_modules/
frontend/dist/
logs/
runtime/
EOF

cd "$PROJECT_ROOT"
uv sync --all-extras

cd "$PROJECT_ROOT/frontend"
npm install
npm run build

pm2 delete llm-council-mcp >/dev/null 2>&1 || true
pm2 delete llm-council-web >/dev/null 2>&1 || true

pm2 start uv --name llm-council-mcp --cwd "$PROJECT_ROOT" --interpreter none -- run python main.py
pm2 start uv --name llm-council-web --cwd "$PROJECT_ROOT" --interpreter none -- run python web_app.py

pm2 save
pm2 startup || true

echo "Setup complete. Dashboard: http://localhost:7842"
echo "If pm2 printed a privileged startup command, run it once and then execute 'pm2 save' again."