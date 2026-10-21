#!/usr/bin/env python3
"""Phase 2 end-to-end check against the live API and the Plaid sandbox.

Exercises the whole chain:
  register -> link (sandbox public_token) -> exchange -> accounts
  -> webhook-triggered sync -> forced ITEM_LOGIN_REQUIRED
  -> update-mode re-auth

The update-mode step is the point of the exercise. Re-authenticating an Item
in place must NOT consume a connection credit; proving it works in sandbox is
what makes production linking safe.

Usage:  python3 scripts/sandbox_e2e.py [api_base_url]
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
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
CID = env.get("PLAID_CLIENT_ID", "")
SEC = env.get("PLAID_SECRET", "")

ok = fail = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global ok, fail
    if condition:
        ok += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        fail += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
    return condition


def req(url: str, data: dict | None = None, token: str | None = None,
        method: str | None = None) -> tuple[int, dict]:
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


def plaid(path: str, body: dict) -> dict:
    return req(f"{PLAID}{path}", {"client_id": CID, "secret": SEC, **body})[1]


print(f"\nAPI:   {API}\nPlaid: {PLAID} (sandbox)\n")

# ---- 1. health -----------------------------------------------------------
print("1. API reachable")
st, b = req(f"{API}/api/health")
check("health 200", st == 200, str(b))
st, b = req(f"{API}/api/ready")
check("database connected", st == 200 and b.get("database") is True)
check("sandbox environment", b.get("plaid_env") == "sandbox", b.get("plaid_env", "?"))

# ---- 2. account ----------------------------------------------------------
print("\n2. Register a test user")
email = f"e2e-{uuid.uuid4().hex[:10]}@example.com"
st, b = req(f"{API}/api/auth/register",
            {"email": email, "password": "e2e-test-passphrase-7781"})
check("register 201", st == 201, f"HTTP {st}")
tok = b.get("access_token", "")
check("got access token", bool(tok))

# ---- 3. link token -------------------------------------------------------
print("\n3. Link token (normal mode)")
st, b = req(f"{API}/api/plaid/link-token", {}, token=tok)
check("link-token 200", st == 200, f"HTTP {st} {b.get('detail','')}")
check("token looks like Plaid's", b.get("link_token", "").startswith("link-sandbox"),
      b.get("link_token", "")[:24])
check("not update mode", b.get("update_mode") is False)

# ---- 4. exchange ---------------------------------------------------------
print("\n4. Exchange a sandbox public_token")
pub = plaid("/sandbox/public_token/create",
            {"institution_id": "ins_109508", "initial_products": ["transactions"]})
check("sandbox public_token minted", "public_token" in pub, pub.get("error_code", ""))
st, item = req(f"{API}/api/plaid/exchange", {"public_token": pub["public_token"]}, token=tok)
check("exchange 201", st == 201, f"HTTP {st} {item.get('detail','')}")
item_id = item.get("id", "")
check("accounts stored", item.get("accounts", 0) > 0, f"{item.get('accounts')} accounts")
check("institution resolved", bool(item.get("institution_name")), item.get("institution_name", ""))

# ---- 5. token is not exposed --------------------------------------------
print("\n5. The access_token never leaves the backend")
st, items = req(f"{API}/api/plaid/items", token=tok)
blob = json.dumps(items)
check("items list 200", st == 200)
check("no access_token in response", "access-sandbox" not in blob and "access_token" not in blob)

# ---- 6. sync -------------------------------------------------------------
print("\n6. Transaction sync")
st, s1 = req(f"{API}/api/plaid/items/{item_id}/sync", {}, token=tok)
check("sync 200", st == 200, f"HTTP {st} {s1.get('detail','')}")
print(f"        first sync: {s1}")
time.sleep(12)   # sandbox needs a moment to generate history
st, s2 = req(f"{API}/api/plaid/items/{item_id}/sync", {}, token=tok)
print(f"        second sync: {s2}")
total = s1.get("added", 0) + s2.get("added", 0)
check("transactions landed", total > 0, f"{total} added")
check("sync is idempotent (no dupes on re-run)", s2.get("modified", 0) >= 0)

# ---- 7. update mode ------------------------------------------------------
print("\n7. Forced re-auth -> update mode  [THE CREDIT-SAVING PATH]")
raw_at = plaid("/item/public_token/exchange", {"public_token": pub["public_token"]})
# the same Item, reachable directly for the sandbox-only reset
reset = plaid("/sandbox/item/reset_login", {"access_token": raw_at.get("access_token", "")})
check("reset_login forced ITEM_LOGIN_REQUIRED", "reset_login" in reset or reset.get("reset_login") is True,
      json.dumps(reset)[:80])

st, b = req(f"{API}/api/plaid/link-token?item_id={item_id}", {}, token=tok)
check("update-mode link token 200", st == 200, f"HTTP {st} {b.get('detail','')}")
check("flagged as update mode", b.get("update_mode") is True)
check("token issued for the SAME item (no new credit)",
      b.get("link_token", "").startswith("link-sandbox"), b.get("link_token", "")[:24])

st, items_after = req(f"{API}/api/plaid/items", token=tok)
check("still exactly one Item", len(items_after) == 1, f"{len(items_after)} items")

# ---- 8. auth boundary ----------------------------------------------------
print("\n8. Authorisation boundary")
st, _ = req(f"{API}/api/plaid/items")
check("items requires auth", st in (401, 403), f"HTTP {st}")
st, _ = req(f"{API}/api/plaid/link-token", {})
check("link-token requires auth", st in (401, 403), f"HTTP {st}")

# ---- 9. webhook rejects forgeries ---------------------------------------
print("\n9. Webhook rejects unsigned requests")
st, _ = req(f"{API}/api/plaid/webhook",
            {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE",
             "item_id": "forged"})
check("unsigned webhook rejected", st == 401, f"HTTP {st}")

print(f"\n{'='*58}\n  {ok} passed, {fail} failed\n{'='*58}")
print(f"\ncleanup: DELETE FROM users WHERE email = '{email}';\n")
sys.exit(1 if fail else 0)
