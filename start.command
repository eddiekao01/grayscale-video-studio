#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Create the virtual environment first; see README.md."
  exit 1
fi

.venv/bin/python manage.py migrate --noinput
echo ""
echo "Open http://127.0.0.1:${PORT:-8001} — press Ctrl+C to stop."
echo ""
exec .venv/bin/python manage.py runserver "127.0.0.1:${PORT:-8001}"
