#!/usr/bin/env python3
"""Grade the compile step of the question pipeline.

Talks to ollama directly with the SAME prompt and schema the app uses,
imported rather than copied -- a prompt that drifts out of sync with
what shipped measures nothing.

Usage:
    python3 experiments/ask-compile/run.py [model]
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai.tasks import COMPILE_PROMPT, _compile_schema  # noqa: E402
from app.analytics.period import resolve  # noqa: E402
from app.analytics.taxonomy import STARTER_TYPES  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
DECK = "http://192.168.86.196:11434"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5vl:7b"

# A plausible account: the standard vocabulary, which is what a user who
# has been labelling for a few weeks would have.
TYPES = [(n, d) for n, d in STARTER_TYPES]
CATEGORIES = ["Tithing", "Rent", "Groceries", "Date Fund", "Discretionary",
              "Health", "Car Insurance", "Short-Term Savings"]


def ask_model(question: str, today: str) -> dict:
    prompt = COMPILE_PROMPT.format(
        today=today,
        question=question,
        types="\n".join(f"  - {n}: {d}" for n, d in TYPES),
        cats="\n".join(f"  - {c}" for c in CATEGORIES),
    )
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": _compile_schema([n for n, _ in TYPES], CATEGORIES),
    }
    req = urllib.request.Request(
        f"{DECK}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        body = json.loads(r.read())
    return json.loads(body["message"]["content"])


def grade(got: dict, want: dict, today: str) -> dict:
    """Per-field verdicts. A list in `expect` means any member passes."""
    out = {}

    out["metric"] = got.get("metric") == want["metric"] if "metric" in want else None

    if "period" in want:
        # Compared as RESOLVED DATES, not as the key. "last_month" and a
        # custom 1--30 September are the same answer asked on 24 October,
        # and marking one wrong would measure phrasing, not correctness.
        try:
            want_p = resolve(
                {"start": want["start"], "end": want["end"]}
                if want["period"] == "custom" else want["period"],
                today=__import__("datetime").date.fromisoformat(today),
            )
            spec = ({"start": got.get("start"), "end": got.get("end")}
                    if got.get("period") == "custom" else got.get("period"))
            got_p = resolve(spec, today=__import__("datetime").date.fromisoformat(today))
            out["period"] = (got_p.start, got_p.end) == (want_p.start, want_p.end)
        except Exception:
            out["period"] = False
    else:
        out["period"] = None

    if "expense_types" in want:
        g = set(got.get("expense_types") or [])
        w = set(want["expense_types"])
        # An empty expectation means "do not filter". Otherwise any
        # overlap counts: "eating out" legitimately maps to one or both
        # food types.
        out["expense_types"] = (g == set()) if w == set() else bool(g & w)
    else:
        out["expense_types"] = None

    if "merchant" in want:
        out["merchant"] = want["merchant"].lower() in (
            got.get("merchant") or "").lower()
    elif "expense_types" in want and want["expense_types"]:
        # Must NOT have dumped a known kind into the merchant field.
        out["merchant"] = not (got.get("merchant") or "").strip()
    else:
        out["merchant"] = None

    out["group_by"] = (
        got.get("group_by") == want["group_by"] if "group_by" in want else None
    )
    return out


def main() -> int:
    cases = json.loads((HERE / "cases.json").read_text())
    rows = []
    t0 = time.time()

    for i, c in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {c['q']}")
        started = time.time()
        try:
            got = ask_model(c["q"], c["today"])
        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED: {exc}")
            rows.append({**c, "got": None, "grade": {}, "error": str(exc)})
            continue
        elapsed = time.time() - started
        g = grade(got, c["expect"], c["today"])
        rows.append({**c, "got": got, "grade": g, "seconds": round(elapsed, 2)})
        marks = " ".join(
            f"{k}={'ok' if v else 'WRONG'}" for k, v in g.items() if v is not None
        )
        print(f"    {marks}  ({elapsed:.1f}s)")

    fields = ("metric", "period", "expense_types", "merchant", "group_by")
    print(f"\n{'field':<16}{'correct':>10}{'graded':>10}")
    print("-" * 36)
    summary = {}
    for f in fields:
        graded = [r for r in rows if r.get("grade", {}).get(f) is not None]
        right = [r for r in graded if r["grade"][f]]
        summary[f] = {"right": len(right), "graded": len(graded)}
        if graded:
            print(f"{f:<16}{len(right):>10}{len(graded):>10}")

    whole = [r for r in rows if r.get("grade") and all(
        v for v in r["grade"].values() if v is not None)]
    print(f"\nfully correct: {len(whole)}/{len(rows)}")
    print(f"model: {MODEL}   total: {time.time() - t0:.0f}s")

    (HERE / "results.json").write_text(json.dumps(
        {"model": MODEL, "summary": summary, "cases": rows}, indent=2))
    print(f"wrote {HERE / 'results.json'}")

    # The period field is the one that fails silently; call it out.
    p = summary.get("period", {})
    if p.get("graded"):
        print(f"\nperiod accuracy is the number that matters: "
              f"{p['right']}/{p['graded']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
