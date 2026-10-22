#!/usr/bin/env python3
"""Phase 3 end-to-end: the budget loop against the live deployment.

Builds the real plan from docs/06, links a sandbox bank, and checks that
spending draws the right categories down and uncertain transactions land in
the review queue instead of being guessed at.

Usage:  python3 scripts/budget_e2e.py [api_base_url]
"""
from __future__ import annotations

import json
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
CID, SEC = env.get("PLAID_CLIENT_ID", ""), env.get("PLAID_SECRET", "")

ok = fail = 0


def check(label, cond, detail=""):
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


print(f"\nBudget loop against {API}\n")

email = f"bg-{uuid.uuid4().hex[:10]}@example.com"
st, b = req(f"{API}/api/auth/register", {"email": email, "password": "budget-e2e-passphrase"})
check("registered", st == 201, f"HTTP {st}")
tok = b.get("access_token", "")

print("\n1. Build the real category set")
cats = {}
spec = [
    ("Tithing", "obligation", None, None),
    ("Rent", "obligation", None, None),
    ("Health", "obligation", None, None),
    ("CarIns", "obligation", None, None),
    ("Food", "spending", 300_00, 200_00),
    ("Savings", "savings_bucket", 2000_00, None),
    ("NewCar", "savings_bucket", None, None),
]
for name, kind, target, alert in spec:
    body = {"name": name, "kind": kind}
    if target:
        body["target_cents"] = target
    if alert:
        body["alert_at_cents"] = alert
    st, c = req(f"{API}/api/budget/categories", body, token=tok)
    if st != 201:
        check(f"create {name}", False, f"HTTP {st} {c}")
        sys.exit(1)
    cats[name] = c
check("seven categories created", len(cats) == 7)

print("\n2. Seed opening balances, then build the split plan")
# Food at 88, savings full, car insurance at 400 -- the docs/06 scenario.
st, plan = req(f"{API}/api/budget/plans", {
    "name": "Standard",
    "rules": [
        {"category_id": cats["Tithing"]["id"], "order_index": 1, "rule_type": "percent",
         "params": {"rate": "0.10", "of": "gross"}},
        {"category_id": cats["Rent"]["id"], "order_index": 2, "rule_type": "fixed",
         "params": {"amount_cents": 350_00}},
        {"category_id": cats["Health"]["id"], "order_index": 3, "rule_type": "fixed",
         "params": {"amount_cents": 150_00}},
        {"category_id": cats["CarIns"]["id"], "order_index": 4, "rule_type": "conditional",
         "params": {"when": {"field": "balance", "op": "<", "value_cents": 450_00},
                    "then": {"rule_type": "fixed", "params": {"amount_cents": 50_00}}}},
        {"category_id": cats["Food"]["id"], "order_index": 5, "rule_type": "refill_to",
         "params": {"target_cents": 300_00}},
        {"category_id": cats["Savings"]["id"], "order_index": 6, "rule_type": "refill_to",
         "params": {"target_cents": 2000_00}},
        {"category_id": cats["NewCar"]["id"], "order_index": 7, "rule_type": "remainder"},
    ],
}, token=tok)
check("plan created", st == 201, f"HTTP {st} {str(plan)[:120]}")
check("plan is active at version 1", plan.get("version") == 1 and plan.get("is_active"))

print("\n3. A malformed plan is refused at save time")
st, bad = req(f"{API}/api/budget/plans", {
    "name": "NoRemainder",
    "rules": [{"category_id": cats["Rent"]["id"], "order_index": 1,
               "rule_type": "fixed", "params": {"amount_cents": 100}}],
}, token=tok)
check("plan without a remainder rejected", st == 422, f"HTTP {st}")

print("\n4. Apply a $4000 paycheck")
st, pc = req(f"{API}/api/budget/paychecks", {"gross_cents": 4000_00}, token=tok)
check("paycheck applied", st == 201 and pc.get("applied"), f"HTTP {st}")
check("every cent allocated", pc.get("allocated_cents") == 4000_00, str(pc.get("allocated_cents")))

st, listing = req(f"{API}/api/budget/categories", token=tok)
bal = {c["name"]: c["balance_cents"] for c in listing}
print("        " + "  ".join(f"{k}={v/100:.2f}" for k, v in bal.items()))
check("tithing is 10% of gross", bal["Tithing"] == 400_00, str(bal["Tithing"]))
check("rent fixed", bal["Rent"] == 350_00)
check("health fixed", bal["Health"] == 150_00)
check("car insurance topped up (was 0, under 450)", bal["CarIns"] == 50_00)
check("food refilled to target", bal["Food"] == 300_00)
check("savings refilled to target", bal["Savings"] == 2000_00)
check("remainder to the car fund",
      bal["NewCar"] == 4000_00 - 400_00 - 350_00 - 150_00 - 50_00 - 300_00 - 2000_00,
      str(bal["NewCar"]))
check("balances sum to gross", sum(bal.values()) == 4000_00, str(sum(bal.values())))

print("\n5. Ledger integrity")
st, s = req(f"{API}/api/budget/summary", token=tok)
check("no ledger drift", s.get("ledger_drift") == [], str(s.get("ledger_drift"))[:100])
check("total matches", s.get("total_allocated_cents") == 4000_00)

print("\n6. Link a bank; transactions categorise on arrival")
st, _ = req(f"{API}/api/budget/rules", {
    "category_id": cats["Food"]["id"], "matcher": {"merchant_contains": "kfc"}, "priority": 10,
}, token=tok)
check("rule created", st == 201, f"HTTP {st}")

pub = plaid("/sandbox/public_token/create",
            {"institution_id": "ins_109508", "initial_products": ["transactions"]})
st, item = req(f"{API}/api/plaid/exchange", {"public_token": pub["public_token"]}, token=tok)
check("bank linked", st == 201, f"HTTP {st}")

added = 0
for _ in range(6):
    st, sy = req(f"{API}/api/plaid/items/{item['id']}/sync", {}, token=tok)
    added += sy.get("added", 0)
    if added:
        break
    time.sleep(10)
check("transactions synced", added > 0, f"{added} added")
check("sync reports categorisation", bool(sy.get("categorised")) or added == 0,
      str(sy.get("categorised")))

print("\n7. Uncertain spending goes to review, not a guess")
st, review = req(f"{API}/api/budget/review", token=tok)
check("review queue populated", len(review) > 0, f"{len(review)} awaiting review")

st, bal2 = req(f"{API}/api/budget/categories", token=tok)
food_now = {c["name"]: c["balance_cents"] for c in bal2}["Food"]
check("the KFC rule drew Food down", food_now < 300_00, f"Food={food_now/100:.2f}")

print("\n8. Resolving a review splits across categories")
if review:
    item0 = review[0]
    amt = item0["amount_cents"]
    half, rest = amt // 2, amt - amt // 2
    st, _ = req(f"{API}/api/budget/review/{item0['id']}/resolve", {
        "splits": [{"category_id": cats["Food"]["id"], "amount_cents": half},
                   {"category_id": cats["NewCar"]["id"], "amount_cents": rest}],
    }, token=tok)
    check("review resolved with a split", st == 204, f"HTTP {st}")

    st, bad = req(f"{API}/api/budget/review/{item0['id']}/resolve", {
        "splits": [{"category_id": cats["Food"]["id"], "amount_cents": amt}],
    }, token=tok)
    check("resolving twice refused", bad and st == 409, f"HTTP {st}")

print("\n9. Ledger still consistent after all of that")
st, s2 = req(f"{API}/api/budget/summary", token=tok)
check("no drift after real traffic", s2.get("ledger_drift") == [], str(s2.get("ledger_drift"))[:120])
check("alerts computed", isinstance(s2.get("alerts"), list))

print(f"\n{'='*58}\n  {ok} passed, {fail} failed\n{'='*58}")
print(f"\ncleanup: DELETE FROM users WHERE email = '{email}';\n")
sys.exit(1 if fail else 0)
