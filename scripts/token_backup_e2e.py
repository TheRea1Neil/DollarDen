#!/usr/bin/env python3
"""Prove the token-backup path survives a total database loss.

Scenario: the Pi dies. A new one is built with the same TOKEN_ENCRYPTION_KEY
(from the password manager) and an empty database. The phone still holds the
encrypted blobs. Can the bank connection be recovered WITHOUT re-linking and
burning a connection credit?

  1. link a sandbox bank
  2. export the encrypted blobs, as the phone would store them
  3. assert the blobs leak nothing usable
  4. DELETE the item from the database -- simulating the loss
  5. restore from the blobs alone
  6. sync again, proving the recovered connection works

Usage:  python3 scripts/token_backup_e2e.py [api_base_url]
"""
from __future__ import annotations

import base64
import json
import pathlib
import time
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

API = sys.argv[1] if len(sys.argv) > 1 else "https://budget.example.org"
PLAID = "https://sandbox.plaid.com"

env = {}
for line in (pathlib.Path(__file__).resolve().parent.parent / ".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
CID, SEC = env.get("PLAID_CLIENT_ID", ""), env.get("PLAID_SECRET", "")

ok = fail = 0


def check(label: str, cond: bool, detail: str = "") -> bool:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        fail += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
    return cond


def req(url, data=None, token=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"))
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw.decode()[:200]}


def plaid(path, body):
    return req(f"{PLAID}{path}", {"client_id": CID, "secret": SEC, **body})[1]


def psql(sql: str) -> str:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "nando",
         f"sudo docker exec dollarden-db psql -U dollarden -d dollarden -At -c \"{sql}\""],
        capture_output=True, text=True, timeout=60).stdout.strip()


print(f"\nDisaster-recovery check against {API}\n")

print("1. Set up: register and link a sandbox bank")
email = f"bk-{uuid.uuid4().hex[:10]}@example.com"
st, b = req(f"{API}/api/auth/register", {"email": email, "password": "backup-test-passphrase-11"})
check("registered", st == 201, f"HTTP {st}")
tok = b.get("access_token", "")
pub = plaid("/sandbox/public_token/create",
            {"institution_id": "ins_109508", "initial_products": ["transactions"]})
st, item = req(f"{API}/api/plaid/exchange", {"public_token": pub["public_token"]}, token=tok)
check("bank linked", st == 201, f"HTTP {st}")
item_id = item.get("id", "")

print("\n2. Export the encrypted blobs (what the phone stores)")
st, backup = req(f"{API}/api/backup/tokens", token=tok)
check("export 200", st == 200, f"HTTP {st}")
check("one blob returned", backup.get("count") == 1, str(backup.get("count")))
check("key fingerprint present", bool(backup.get("key_fingerprint")), backup.get("key_fingerprint", ""))
blobs = backup.get("items", [])

print("\n3. The blob must leak nothing usable")
raw = json.dumps(backup)
check("no plaintext token in the payload", "access-sandbox" not in raw)
if blobs:
    decoded = base64.b64decode(blobs[0]["blob_b64"])
    check("decoded ciphertext has no token", b"access-sandbox" not in decoded)
    # Fernet.encrypt() returns URL-SAFE BASE64 TEXT; the 0x80 version byte
    # sits one layer down, after decoding that. Checking the outer layer for
    # 0x80 tests the wrong thing.
    import base64 as _b64

    inner = _b64.urlsafe_b64decode(decoded)
    check("ciphertext is Fernet-shaped", inner[:1] == b"\x80",
          f"{len(decoded)}B outer, version={inner[:1]!r}")

print("\n4. Simulate catastrophic loss: delete the item from the database")
plaid_item_id = blobs[0]["plaid_item_id"] if blobs else ""
psql(f"DELETE FROM plaid_items WHERE plaid_item_id = '{plaid_item_id}';")
remaining = psql(f"SELECT COUNT(*) FROM plaid_items WHERE plaid_item_id = '{plaid_item_id}';")
check("item is gone from the database", remaining == "0", f"count={remaining}")
st, items_now = req(f"{API}/api/plaid/items", token=tok)
check("API agrees it is gone", len(items_now) == 0, f"{len(items_now)} items")

print("\n5. Restore from the blobs alone  [NO RE-LINK, NO CREDIT SPENT]")
st, res = req(f"{API}/api/backup/tokens", {"items": blobs}, token=tok)
check("restore 200", st == 200, f"HTTP {st} {res.get('detail','')}")
check("one item restored", res.get("restored") == 1, json.dumps(res.get("results", []))[:120])

print("\n6. The recovered connection actually works")
st, items_after = req(f"{API}/api/plaid/items", token=tok)
check("item is back", len(items_after) == 1, f"{len(items_after)} items")
new_id = items_after[0]["id"] if items_after else ""
# The sandbox generates history asynchronously after an Item is created, so
# a sync moments later legitimately returns nothing. Poll rather than
# asserting on the first attempt.
added = 0
for attempt in range(6):
    st, s = req(f"{API}/api/plaid/items/{new_id}/sync", {}, token=tok)
    added += s.get("added", 0)
    if added:
        break
    time.sleep(10)
check("sync works after restore", st == 200, f"HTTP {st}")
check("transactions re-pulled after restore", added > 0,
      f"{added} added after {attempt + 1} attempt(s)")

print("\n7. A blob sealed with a different key is refused")
bogus = dict(blobs[0], key_fingerprint="0000000000000000")
st, res2 = req(f"{API}/api/backup/tokens", {"items": [bogus], "overwrite_existing": True}, token=tok)
outcome = (res2.get("results") or [{}])[0].get("outcome")
check("wrong-key blob rejected", outcome == "wrong_key", str(outcome))
check("nothing restored from it", res2.get("restored") == 0)

print("\n8. Backup endpoints require authentication")
st, _ = req(f"{API}/api/backup/tokens")
check("export needs auth", st in (401, 403), f"HTTP {st}")

print(f"\n{'='*58}\n  {ok} passed, {fail} failed\n{'='*58}")
print(f"\ncleanup: DELETE FROM users WHERE email = '{email}';\n")
sys.exit(1 if fail else 0)
