#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ ! -x .venv/bin/python ]; then
  echo "Create the virtual environment first; see README.md."
  exit 1
fi

.venv/bin/python manage.py migrate --noinput
echo ""
echo "Open http://127.0.0.1:${PORT:-8001} — press Ctrl+C to stop."
echo ""
.venv/bin/python manage.py process_videos &
video_worker_pid=$!
# Stop both processes when the launcher exits or receives an interrupt.
cleanup() {
  kill "$video_worker_pid" "${video_server_pid:-}" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM
.venv/bin/python manage.py runserver "127.0.0.1:${PORT:-8001}" &
video_server_pid=$!
wait "$video_server_pid"
