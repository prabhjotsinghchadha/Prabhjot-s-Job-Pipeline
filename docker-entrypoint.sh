#!/bin/sh
# Container entrypoint. Data lives in DATA_DIR when set (mount a volume there),
# otherwise in /app where docker-compose bind-mounts profile.yaml, resumes/,
# applications.db and .cache/.
set -e
if [ -n "$DATA_DIR" ]; then
  mkdir -p "$DATA_DIR/.cache" "$DATA_DIR/resumes"
fi
exec python main.py server --host 0.0.0.0 --port "${PORT:-8080}"
