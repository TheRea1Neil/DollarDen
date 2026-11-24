#!/usr/bin/env bash
# Restore the DollarDen database from a dump produced by backup.sh.
#
# This is the disaster path: it REPLACES the live database. backup.sh
# already proves restorability nightly against a scratch database, so
# reach for this only when the real one is gone or wrong.
#
#   ./restore.sh                      # newest dump, after confirmation
#   ./restore.sh backups/dollarden-X.dump   # a specific one
#   DOLLARDEN_RESTORE_YES=1 ./restore.sh    # no prompt (for a known-good rerun)
#
# Afterwards the API needs restarting so SQLAlchemy drops stale
# connections to a database that no longer exists underneath it.
set -euo pipefail

ENV_FILE="${DOLLARDEN_ENV_FILE:-$HOME/dollarden/.env}"
OUT_DIR="${DOLLARDEN_BACKUP_DIR:-$HOME/dollarden/deploy/backups}"
CONTAINER="${DOLLARDEN_PG_CONTAINER:-dollarden-db-1}"
API="${DOLLARDEN_API_CONTAINER:-dollarden-api-1}"

now() { date +%Y-%m-%dT%H:%M:%S%z; }
# systemd runs this as root; a human runs it as neil, who is not in the
# docker group. A function, not a variable: dkr holding "sudo
# docker" is one quoted word and fails as a command, and leaving it
# unquoted invites word-splitting bugs later.
if docker ps >/dev/null 2>&1; then
  dkr() { docker "$@"; }
else
  dkr() { sudo docker "$@"; }
fi

log() { printf '%s dollarden-restore: %s\n' "$(now)" "$*"; }
die() { log "FAILED: $*" >&2; exit 1; }

DB="$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2-)"
USR="$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2-)"

SRC="${1:-}"
if [[ -z "$SRC" ]]; then
  SRC="$(ls -1t "$OUT_DIR"/dollarden-*.dump 2>/dev/null | head -1 || true)"
  [[ -n "$SRC" ]] || die "no dumps in $OUT_DIR"
fi
[[ -s "$SRC" ]] || die "$SRC is missing or empty"
BASE="$(basename "$SRC")"

# The dump holds access_tokens as ciphertext. Restoring a database
# without the matching TOKEN_ENCRYPTION_KEY gives back a budget and
# zero working bank connections -- and relinking costs a Plaid credit
# that is never returned. Refuse to proceed silently without it.
grep -qE '^TOKEN_ENCRYPTION_KEY=.+' "$ENV_FILE" \
  || die "TOKEN_ENCRYPTION_KEY is not set in $ENV_FILE -- restoring now would
        produce a database whose bank tokens cannot be decrypted. Put the
        key back first (password manager), then rerun."

log "about to REPLACE database '$DB' with $BASE"
if [[ "${DOLLARDEN_RESTORE_YES:-}" != "1" ]]; then
  read -r -p "Type the database name to confirm: " answer
  [[ "$answer" == "$DB" ]] || die "not confirmed"
fi

# Stop the API first: an open pool blocks DROP DATABASE and, worse, can
# write into the half-restored database mid-flight.
log "stopping $API"
dkr stop "$API" >/dev/null 2>&1 || log "note: $API was not running"

# Keep the current state under a name rather than destroying it. If the
# dump turns out to be the wrong one, the mistake is still reversible.
SAFETY="${DB}_before_restore_$(date +%Y%m%d%H%M%S)"
log "renaming current '$DB' to '$SAFETY' instead of dropping it"
dkr exec "$CONTAINER" psql -U "$USR" -d postgres -v ON_ERROR_STOP=1 -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB' AND pid<>pg_backend_pid()" >/dev/null
dkr exec "$CONTAINER" psql -U "$USR" -d postgres -v ON_ERROR_STOP=1 -c \
  "ALTER DATABASE \"$DB\" RENAME TO \"$SAFETY\"" >/dev/null || die "could not set the old database aside"

dkr exec "$CONTAINER" createdb -U "$USR" "$DB" || die "could not create a fresh $DB"

# Copy the dump in if it is not already on the bind mount.
INNER="/backups/$BASE"
if [[ "$(cd "$(dirname "$SRC")" && pwd)" != "$OUT_DIR" ]]; then
  dkr cp "$SRC" "$CONTAINER:$INNER" || die "could not copy the dump into the container"
fi

log "restoring $BASE"
dkr exec "$CONTAINER" pg_restore -U "$USR" -d "$DB" --no-owner --no-privileges "$INNER" \
  || log "pg_restore reported warnings"

for t in users plaid_items accounts transactions categories; do
  n="$(dkr exec "$CONTAINER" psql -U "$USR" -d "$DB" -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo ERR)"
  log "  $t: $n"
done

log "starting $API"
dkr start "$API" >/dev/null 2>&1 || log "note: $API could not be started -- start it yourself"

log "done. Old database kept as '$SAFETY' -- drop it once you are satisfied:"
log "  dkr exec $CONTAINER dropdb -U $USR $SAFETY"
