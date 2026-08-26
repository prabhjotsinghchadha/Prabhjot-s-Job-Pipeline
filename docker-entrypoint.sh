#!/bin/sh
# Container entrypoint.
#
# Two deployment shapes:
#   Local (docker-compose): profile.yaml, applications.db, .cache/ and
#     resumes/ are bind-mounted into /app. DATA_DIR is unset — no-op here.
#   Hosted (Railway etc.): a single persistent volume is mounted somewhere
#     (e.g. /data) and DATA_DIR points at it. All mutable state is symlinked
#     onto the volume so deploys and restarts never lose data. SQLite
#     resolves symlinks for its -wal/-shm side files, so WAL mode is safe.
#
# PORT is honored for platforms that inject it (Railway); defaults to 8080.
set -e

if [ -n "$DATA_DIR" ]; then
  mkdir -p "$DATA_DIR/.cache" "$DATA_DIR/resumes"

  for f in applications.db profile.yaml; do
    # Seed the volume from an image copy on first boot (normally absent —
    # .dockerignore excludes both — but harmless if a local build has them).
    if [ -f "/app/$f" ] && [ ! -L "/app/$f" ] && [ ! -e "$DATA_DIR/$f" ]; then
      cp "/app/$f" "$DATA_DIR/$f"
    fi
    rm -f "/app/$f"
    ln -s "$DATA_DIR/$f" "/app/$f"
  done

  for d in .cache resumes; do
    if [ -d "/app/$d" ] && [ ! -L "/app/$d" ]; then
      cp -a "/app/$d/." "$DATA_DIR/$d/" 2>/dev/null || true
      rm -rf "/app/$d"
    fi
    ln -sfn "$DATA_DIR/$d" "/app/$d"
  done
fi

exec python main.py server --port "${PORT:-8080}"
