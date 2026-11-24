#!/usr/bin/env bash
# Nightly backup of the DollarDen database.
#
# This exists because of a Plaid rule, not general tidiness. The Trial
# plan caps the account at 10 Items for its LIFETIME, and /item/remove
# does not give a slot back (docs/04). The access_token for a linked
# bank lives only in this database. Lose it and relinking Bank of
# America permanently burns one of ten. There is no support ticket for
# that.
#
# So this script does not just write a dump -- it RESTORES the dump into
# a scratch database and compares row counts before calling it good. A
# backup nobody has read is not a backup, and the failure everyone hits
# is the one that writes a well-formed empty file every night for a
# year. Verifying nightly also keeps the docs/04 cutover gate ("a
# restore has been performed") permanently true rather than true once.
#
# Install: see docs/09-pi-operations.md.
set -euo pipefail

ENV_FILE="${DOLLARDEN_ENV_FILE:-$HOME/dollarden/.env}"
OUT_DIR="${DOLLARDEN_BACKUP_DIR:-$HOME/dollarden/deploy/backups}"
CONTAINER="${DOLLARDEN_PG_CONTAINER:-dollarden-db-1}"
KEEP_DAYS="${DOLLARDEN_BACKUP_KEEP:-14}"

# Tables whose emptiness means the backup is worthless. plaid_items is
# the irreplaceable one; the others would merely be painful.
CRITICAL_TABLES=(users plaid_items accounts transactions categories)

# Portable spellings throughout: the Pi is GNU, but being runnable on a
# Mac is what makes this script testable before it is trusted.
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

log() { printf '%s dollarden-backup: %s\n' "$(now)" "$*"; }
die() { log "FAILED: $*" >&2; exit 1; }

[[ -r "$ENV_FILE" ]] || die "cannot read $ENV_FILE"
DB="$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2-)"
USR="$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2-)"
[[ -n "$DB" && -n "$USR" ]] || die "POSTGRES_DB/POSTGRES_USER missing from $ENV_FILE"

dkr inspect "$CONTAINER" >/dev/null 2>&1 || die "container $CONTAINER not found"

mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="dollarden-${STAMP}.dump"

# -Fc (custom format) rather than plain SQL: pg_restore can then be
# selective, and it compresses. The container writes it to the bind
# mount, so the file lands in $OUT_DIR on the host.
log "dumping $DB"
dkr exec "$CONTAINER" pg_dump -U "$USR" -d "$DB" -Fc -f "/backups/$NAME" \
  || die "pg_dump returned non-zero"

[[ -s "$OUT_DIR/$NAME" ]] || die "$NAME is empty or missing on the host"
SIZE="$(wc -c < "$OUT_DIR/$NAME" | tr -d " ")"

# ---- verify by actually restoring it ------------------------------- #
VDB="dollarden_verify_$$"
cleanup() {
  dkr exec "$CONTAINER" dropdb -U "$USR" --if-exists "$VDB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "restoring into scratch db $VDB"
dkr exec "$CONTAINER" createdb -U "$USR" "$VDB" || die "could not create $VDB"
# pg_restore warns about things like missing roles and still succeeds;
# only a hard failure matters, so capture output and judge on the
# comparison below rather than on exit status alone.
dkr exec "$CONTAINER" pg_restore -U "$USR" -d "$VDB" --no-owner --no-privileges \
  "/backups/$NAME" 2>/tmp/dollarden-restore-warn.$$ || log "pg_restore reported warnings"

count() {  # count <database> <table>
  dkr exec "$CONTAINER" psql -U "$USR" -d "$1" -tAc \
    "SELECT count(*) FROM $2" 2>/dev/null || echo "ERR"
}

MISMATCH=0
FOUND=0
SUMMARY=""
for t in "${CRITICAL_TABLES[@]}"; do
  live="$(count "$DB" "$t")"
  back="$(count "$VDB" "$t")"
  [[ "$live" == "ERR" ]] && { log "note: $t absent from live db, skipping"; continue; }
  FOUND=$((FOUND + 1))
  SUMMARY+="  $t: live=$live restored=$back"$'\n'
  if [[ "$live" != "$back" ]]; then
    MISMATCH=1
    log "MISMATCH $t: live=$live restored=$back"
  fi
done

printf '%s' "$SUMMARY"

# Every critical table missing does not mean "nothing to back up" -- it
# means this is pointing at the wrong database, or at one that never got
# migrated. Backing that up nightly and reporting OK is the exact silent
# failure this script exists to prevent, so refuse rather than continue.
(( FOUND > 0 )) || die "none of (${CRITICAL_TABLES[*]}) exist in '$DB' -- wrong database, or migrations never ran"
(( MISMATCH == 0 )) || die "restored row counts do not match the live database"

# A connection that cannot be decrypted after a restore is the whole
# nightmare this guards against, so assert the ciphertext survived
# byte-for-byte rather than trusting the row count.
if [[ "$(count "$DB" plaid_items)" =~ ^[1-9] ]]; then
  same="$(dkr exec "$CONTAINER" psql -U "$USR" -d "$DB" -tAc \
    "SELECT md5(string_agg(encode(access_token_encrypted,'hex'), '' ORDER BY id::text)) FROM plaid_items")"
  rsame="$(dkr exec "$CONTAINER" psql -U "$USR" -d "$VDB" -tAc \
    "SELECT md5(string_agg(encode(access_token_encrypted,'hex'), '' ORDER BY id::text)) FROM plaid_items")"
  [[ "$same" == "$rsame" ]] || die "access_token ciphertext differs after restore"
  log "access_token ciphertext verified identical"
else
  log "note: no plaid_items yet -- ciphertext check skipped"
fi

# ---- retention ------------------------------------------------------ #
find "$OUT_DIR" -name 'dollarden-*.dump' -type f -mtime "+$KEEP_DAYS" -print -delete \
  | while read -r old; do log "pruned $(basename "$old")"; done

log "OK $NAME ($(awk -v b="$SIZE" 'BEGIN{printf "%.1f MiB", b/1048576}')) verified by restore"

# The dump is ciphertext for the part that matters: access_tokens are
# encrypted with TOKEN_ENCRYPTION_KEY, which lives in .env and is NOT in
# this file. Backing up the database without that key restores a budget
# and loses every bank connection. Keep the key in a password manager.
