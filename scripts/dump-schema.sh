#!/usr/bin/env bash
# Regenerate docs/schema/schema.sql from the live database.
#
# Dumped rather than hand-written: a schema doc that drifts from the
# database is worse than none, because it is believed.
set -euo pipefail
HOST="${DOLLARDEN_HOST:-nando}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/docs/schema/schema.sql"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

ssh "$HOST" 'sudo docker exec dollarden-db pg_dump -U dollarden -d dollarden \
  --schema-only --no-owner --no-privileges' > "$TMP"
[[ -s "$TMP" ]] || { echo "empty dump -- is the container running?" >&2; exit 1; }

{
  sed -n '1,/^$/p' "$OUT" 2>/dev/null | head -16   # keep the existing header
  sed '/^--$/d; /^-- Dumped/d; /^-- PostgreSQL database dump/d; /^SET /d; /^SELECT pg_catalog.set_config/d; /^\\restrict/d; /^\\unrestrict/d' "$TMP" \
    | cat -s \
    | python3 "$(dirname "$0")/_annotate_schema.py"
} > "$OUT.new"
mv "$OUT.new" "$OUT"
echo "wrote $OUT ($(grep -c '^CREATE TABLE' "$OUT") tables)"
