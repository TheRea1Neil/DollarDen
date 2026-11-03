#!/usr/bin/env bash
# Switch nnf from the Plaid sandbox to production.
#
# Run this ON THE PI:  ~/nnf/deploy/go-production.sh
#
# The production secret is typed into a hidden prompt and goes straight
# into .env. It never appears in this transcript, in your shell history,
# in the process list (argv is world-readable via ps), in a log, or
# anywhere near the git repo. That is the whole reason this is a script
# and not three sed commands.
#
# Nothing is written until the secret has been proven to work against
# Plaid, so a typo costs nothing and leaves the box on sandbox.
set -euo pipefail

ENV_FILE="${NNF_ENV_FILE:-/home/neil/nnf/.env}"
HERE="$(cd "$(dirname "$0")" && pwd)"
API="${NNF_API_CONTAINER:-nnf-api}"

if docker ps >/dev/null 2>&1; then dkr() { docker "$@"; }; else dkr() { sudo docker "$@"; }; fi

log() { printf '  %s\n' "$*"; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }
rule() { printf '\n%s\n' "────────────────────────────────────────────────────────"; }

get() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

# --- preflight ------------------------------------------------------- #
rule; echo "Preflight"
[[ -w "$ENV_FILE" ]] || die "cannot write $ENV_FILE"

CLIENT_ID="$(get PLAID_CLIENT_ID)"
[[ -n "$CLIENT_ID" ]] || die "PLAID_CLIENT_ID is not set"
log "client id        ${CLIENT_ID:0:8}… (same in both environments)"

# Without this key the Plaid tokens in the database -- the ones about to
# become real -- cannot be decrypted, and a restore gives back a budget
# with zero working bank connections. Relinking costs a credit that is
# never returned, so refuse to go live without it.
[[ -n "$(get TOKEN_ENCRYPTION_KEY)" ]] || die "TOKEN_ENCRYPTION_KEY is not set. Set it (and store a copy
        in a password manager) before linking a real bank."
log "encryption key   present"

CURRENT_ENV="$(get PLAID_ENV)"
log "current env      $CURRENT_ENV"
if [[ "$CURRENT_ENV" == "production" && "${1:-}" != "--force" ]]; then
  echo; echo "Already on production. Re-run with --force to replace the secret."; exit 0
fi

PKG="$(get PLAID_ANDROID_PACKAGE_NAME)"
[[ -n "$PKG" ]] && log "android package  $PKG" \
  || log "android package  (unset -- Android OAuth will refuse to mint a token)"

# --- a restore point before anything changes -------------------------- #
rule; echo "Taking a verified backup first"
if [[ -x "$HERE/backup.sh" ]]; then
  "$HERE/backup.sh" >/dev/null 2>&1 && log "backup ok (verified by restore)" \
    || die "backup failed -- not switching to production without a restore point"
else
  log "backup.sh not found; skipping"
fi

# --- the secret ------------------------------------------------------- #
rule
cat <<'TXT'
Paste the PRODUCTION secret from the Plaid dashboard
(Home -> Keys -> Production -> Secret).

Input is hidden. Nothing is written until Plaid accepts it.
TXT
printf '\nProduction secret: '
read -rs SECRET
printf '\n'
[[ -n "$SECRET" ]] || die "nothing entered"
[[ "$SECRET" != *[[:space:]]* ]] || die "the secret contains whitespace -- check what was pasted"

SANDBOX_SECRET="$(get PLAID_SECRET)"
[[ "$SECRET" != "$SANDBOX_SECRET" ]] \
  || die "that is the sandbox secret already in .env, not the production one"

# --- prove it works BEFORE writing anything --------------------------- #
rule; echo "Checking the secret against Plaid production"

# The secret goes to curl on stdin, never as an argument: argv is
# visible to every user on the box through ps.
plaid_post() {  # plaid_post <path> <extra-json>
  python3 -c '
import json, sys
cid, extra = sys.argv[1], sys.argv[2]
secret = sys.stdin.readline().rstrip("\n")
body = {"client_id": cid, "secret": secret}
body.update(json.loads(extra))
sys.stdout.write(json.dumps(body))
' "$CLIENT_ID" "$2" <<<"$SECRET" \
  | curl -s --max-time 30 -H 'content-type: application/json' --data @- \
      "https://production.plaid.com$1"
}

RESP="$(plaid_post /institutions/get '{"count":1,"offset":0,"country_codes":["US"]}')" \
  || die "could not reach production.plaid.com"

if grep -q '"error_code"' <<<"$RESP"; then
  CODE="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("error_code",""))' <<<"$RESP")"
  MSG="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("error_message",""))' <<<"$RESP")"
  echo; echo "Plaid rejected it: $CODE"; echo "  $MSG"
  die "nothing was changed -- still on $CURRENT_ENV"
fi
log "credentials accepted by production"

# A link token is free; only exchanging a public_token creates an Item
# and spends a credit. So this proves the Android OAuth configuration
# works in production BEFORE you ever open Link.
if [[ -n "$PKG" ]]; then
  LT="$(plaid_post /link/token/create "$(python3 -c '
import json,sys
print(json.dumps({
  "client_name": "no-nonsense-finance", "language": "en",
  "country_codes": ["US"], "user": {"client_user_id": "preflight"},
  "products": ["transactions"], "android_package_name": sys.argv[1],
}))' "$PKG")")"
  if grep -q '"error_code"' <<<"$LT"; then
    CODE="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("error_code",""))' <<<"$LT")"
    MSG="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("error_message",""))' <<<"$LT")"
    echo; echo "Production link token for Android failed: $CODE"; echo "  $MSG"
    die "nothing was changed -- fix the dashboard registration first"
  fi
  log "android production link token minted (no credit used)"
fi

# --- write ------------------------------------------------------------ #
rule; echo "Updating $ENV_FILE"
STAMP="$(date +%Y%m%d-%H%M%S)"
BAK="${ENV_FILE}.bak-${STAMP}"
(umask 077; cp -p "$ENV_FILE" "$BAK")
log "backup           $BAK"

python3 -c "
import sys, pathlib
secret = sys.stdin.readline().rstrip(chr(10))
path = pathlib.Path(sys.argv[1])
want = {
    \"PLAID_SECRET\": secret,
    \"PLAID_ENV\": \"production\",
    \"ALLOW_PRODUCTION\": \"true\",
}
out, seen = [], set()
for line in path.read_text().splitlines():
    stripped = line.lstrip()
    key = line.split(chr(61), 1)[0].strip() if (chr(61) in line and not stripped.startswith(chr(35))) else None
    if key in want:
        out.append(key + chr(61) + want[key])
        seen.add(key)
    else:
        out.append(line)
for key in want:
    if key not in seen:
        out.append(key + chr(61) + want[key])
path.write_text(chr(10).join(out) + chr(10))
" "$ENV_FILE" <<<"$SECRET"
unset SECRET
chmod 600 "$ENV_FILE"
log "PLAID_SECRET     replaced"
log "PLAID_ENV        production"
log "ALLOW_PRODUCTION true"

# --- restart and verify ------------------------------------------------ #
rule; echo "Restarting the API"
dkr restart "$API" >/dev/null || die "could not restart $API"
for i in $(seq 1 30); do
  sleep 2
  state="$(dkr inspect -f '{{.State.Health.Status}}' "$API" 2>/dev/null || echo unknown)"
  [[ "$state" == "healthy" ]] && break
done
[[ "${state:-}" == "healthy" ]] || die "$API did not come back healthy.
        Roll back with:  cp $BAK $ENV_FILE && sudo docker restart $API"
log "api healthy"

RUNNING_ENV="$(dkr exec "$API" python -c \
  'from app.core.config import get_settings; print(get_settings().plaid_env)' 2>/dev/null || echo '?')"
[[ "$RUNNING_ENV" == "production" ]] \
  || die "the API is running with plaid_env=$RUNNING_ENV, not production"
log "api reports      plaid_env=$RUNNING_ENV"

rule
cat <<TXT
Live on production.

Rollback, if you want it:
  cp $BAK $ENV_FILE && sudo docker restart $API

Still to do, deliberately NOT done here:

  * TOKEN_ENCRYPTION_KEY into a password manager, if it is not already.
    Backups restore ciphertext; without that key a restored database
    has no working bank connections.

  * Sandbox Items are still in the database. They are dead now -- their
    tokens point at the sandbox host. Deleting them is destructive in a
    non-obvious way: accounts, transactions AND allocations cascade, so
    category balances move. Decide separately, after a backup.

  * Link ONE bank, then watch a full pay cycle before linking another.
    Each link spends one of ten, for the lifetime of the account.
TXT
