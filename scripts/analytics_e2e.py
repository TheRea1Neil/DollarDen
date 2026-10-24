#!/usr/bin/env python3
"""Analytics end-to-end against the live deployment.

Unit tests run against a container; this runs against the Pi, the real
Caddy edge and the real database. Every significant bug on this project
has surfaced here with the unit suite green.

Usage:  python3 scripts/analytics_e2e.py [api_base_url]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid

API = sys.argv[1] if len(sys.argv) > 1 else "https://budget.example.org"
ok = fail = 0


def call(method, path, body=None, token=None, expect=200):
    req = urllib.request.Request(
        f"{API}/api{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    data = json.loads(payload) if payload else None
    assert code == expect, f"{method} {path} -> {code} (wanted {expect}): {payload[:300]}"
    return data


def check(label, fn):
    global ok, fail
    try:
        fn()
        print(f"  ok    {label}")
        ok += 1
    except AssertionError as e:
        print(f"  FAIL  {label}\n        {e}")
        fail += 1


def register() -> str:
    """Registration is rate limited; back off rather than giving up."""
    for attempt in range(6):
        try:
            d = call("POST", "/auth/register",
                     {"email": f"an-{uuid.uuid4().hex[:10]}@example.com",
                      "password": "analytics-e2e-passphrase-99"}, expect=201)
            return d["access_token"]
        except AssertionError as e:
            if "429" not in str(e):
                raise
            time.sleep(20 * (attempt + 1))
    raise AssertionError("rate limited for too long")


def main() -> int:
    print(f"analytics e2e against {API}")
    token = register()
    print("  ok    registered")

    def empty_insights():
        d = call("GET", "/analytics/insights?period=this_month", token=token)
        kinds = [c["kind"] for c in d["cards"]]
        assert kinds == ["empty"], f"fresh account should say so, got {kinds}"
        assert d["period"]["start"].endswith("-01"), d["period"]

    def bad_period_is_400():
        call("GET", "/analytics/insights?period=whenever", token=token, expect=400)

    def client_supplied_today_is_honoured():
        d = call("GET", "/analytics/insights?period=last_month&today=2026-03-15",
                 token=token)
        assert d["period"]["start"] == "2026-02-01", d["period"]
        assert d["period"]["end"] == "2026-02-28", d["period"]

    def create_type():
        d = call("POST", "/analytics/expense-types",
                 {"name": "Fast food", "description": "Drive-through and counter service"},
                 token=token, expect=201)
        assert d["reviewed"] is True, d

    def duplicate_guard():
        d = call("POST", "/analytics/expense-types", {"name": "Fast Foods"},
                 token=token, expect=409)
        assert "Fast food" in d["detail"], d

    def list_types():
        d = call("GET", "/analytics/expense-types", token=token)
        assert [t["name"] for t in d] == ["Fast food"], d

    def suggestions_offer_standards():
        d = call("GET", "/analytics/expense-types/suggestions", token=token)
        by = {s["name"]: s["status"] for s in d["suggestions"]}
        assert by.get("Fast food") == "existing", by
        assert by.get("Subscriptions") == "available", by

    def spend_is_zero_not_an_error():
        d = call("GET", "/analytics/spend?period=last_30_days", token=token)
        assert d["total_cents"] == 0 and d["count"] == 0, d

    def grouping_works_on_an_empty_ledger():
        d = call("GET", "/analytics/spend?group_by=expense_type", token=token)
        assert d["groups"] == [], d

    def recurring_on_no_history():
        d = call("GET", "/analytics/recurring", token=token)
        assert d["active_count"] == 0 and d["items"] == [], d

    def untyped_queue_is_empty():
        d = call("GET", "/analytics/untyped", token=token)
        assert d["transactions"] == [], d

    def merge_moves_a_type_away():
        a = call("GET", "/analytics/expense-types", token=token)[0]
        b = call("POST", "/analytics/expense-types", {"name": "Takeaway"},
                 token=token, expect=201)
        call("POST", f"/analytics/expense-types/{b['id']}/merge",
             {"into_id": a["id"]}, token=token)
        names = [t["name"] for t in call("GET", "/analytics/expense-types", token=token)]
        assert names == ["Fast food"], names

    def another_users_data_is_invisible():
        other = register()
        d = call("GET", "/analytics/expense-types", token=other)
        assert d == [], f"cross-user leak: {d}"

    def unauthenticated_is_refused():
        call("GET", "/analytics/insights", expect=401)

    for label, fn in [
        ("fresh account says 'nothing yet'", empty_insights),
        ("an unparseable period is a 400", bad_period_is_400),
        ("the phone's `today` decides the month", client_supplied_today_is_honoured),
        ("create an expense type", create_type),
        ("a near-duplicate is refused by name", duplicate_guard),
        ("list types", list_types),
        ("standards are offered, not created", suggestions_offer_standards),
        ("empty ledger returns zero, not an error", spend_is_zero_not_an_error),
        ("grouping on an empty ledger", grouping_works_on_an_empty_ledger),
        ("recurring with no history", recurring_on_no_history),
        ("untyped queue empty", untyped_queue_is_empty),
        ("merge folds one type into another", merge_moves_a_type_away),
        ("another user sees nothing", another_users_data_is_invisible),
        ("no token is a 401", unauthenticated_is_refused),
    ]:
        check(label, fn)

    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
