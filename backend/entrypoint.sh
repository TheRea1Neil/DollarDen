#!/bin/sh
# Bring the schema up to date, then serve.
#
# Without this a fresh install returns 500 to everything until someone
# works out that `alembic upgrade head` exists -- which was measured,
# not guessed: a clean `docker compose up` answered "Internal Server
# Error" on every endpoint, with nothing to suggest why. The same trap
# springs on every later upgrade, where a new image meets a schema it
# no longer matches.
#
# Failure stops the container rather than serving a half-migrated
# database. A loud crash with the alembic error in the log is far
# easier to diagnose than an app that mostly works.
#
# Assumes one instance at a time. Alembic takes no lock of its own, so
# two containers starting together could both try to migrate; if this
# is ever run with replicas, that needs an advisory lock first.
set -e

if [ "${DOLLARDEN_SKIP_MIGRATIONS:-0}" = "1" ]; then
    echo "entrypoint: DOLLARDEN_SKIP_MIGRATIONS=1, leaving the schema alone"
else
    echo "entrypoint: applying database migrations"
    alembic upgrade head
fi

exec "$@"
