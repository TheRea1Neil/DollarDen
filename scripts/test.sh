#!/usr/bin/env bash
# Run the test suite in a container built from pyproject.toml ALONE.
#
# Why not just `pytest` in a local venv: a venv accumulates packages from
# earlier experiments, so a missing dependency declaration stays invisible
# until deployment. This caught exactly that -- `pydantic[email]` was used by
# the code but declared as plain `pydantic`, and only surfaced on the Pi.
#
# Installing with `pip install .[dev]` resolves the same dependency set the
# Dockerfile does, so a missing extra fails here instead of in production.
set -euo pipefail

cd "$(dirname "$0")/.."
PG_NAME=nnf-pg-test
PG_PORT=${PG_PORT:-55433}

cleanup() { [[ "${KEEP_PG:-0}" == "1" ]] || docker rm -f "$PG_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if ! docker inspect "$PG_NAME" >/dev/null 2>&1; then
  echo "==> starting Postgres on :$PG_PORT"
  docker run -d --name "$PG_NAME" \
    -e POSTGRES_PASSWORD=devpw -e POSTGRES_USER=nnf -e POSTGRES_DB=nnf \
    -p "$PG_PORT":5432 postgres:16-alpine >/dev/null
fi
until docker exec "$PG_NAME" pg_isready -U nnf -d nnf >/dev/null 2>&1; do sleep 1; done
echo "==> Postgres ready"

echo "==> installing from pyproject.toml only, then testing"
docker run --rm --network host -v "$PWD/backend":/src -w /src \
  -e POSTGRES_HOST=127.0.0.1 -e POSTGRES_PORT="$PG_PORT" \
  -e POSTGRES_USER=nnf -e POSTGRES_PASSWORD=devpw -e POSTGRES_DB=nnf \
  -e ALLOW_OPEN_REGISTRATION=true \
  python:3.11-slim-bookworm bash -c '
    set -e
    apt-get update -qq && apt-get install -y -qq --no-install-recommends build-essential >/dev/null
    pip install -q ".[dev]"
    alembic upgrade head
    python -m pytest tests/ -q
  '
