#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
exec "$project_root/.venv/bin/python" -m backend.server --host 127.0.0.1 --port 8080 --data-dir "$project_root/data" "$@"
