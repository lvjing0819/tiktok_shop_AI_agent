#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  selected_python=''
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      selected_python="$candidate"
      break
    fi
  done
  if [ -z "$selected_python" ]; then
    echo '请先安装 Python 3.11 或更新版本。'
    exit 1
  fi
  "$selected_python" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.lock
if [ ! -f .env ]; then cp .env.example .env; fi
(cd frontend && npm ci && npm run build)
exec .venv/bin/python -m scripts.run
