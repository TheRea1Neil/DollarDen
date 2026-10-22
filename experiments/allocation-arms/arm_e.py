"""Arm E: the engine allocates, the model reviews.

The strongest version of "descriptions drive allocation" that does not put
a language model in the money path. The engine produces the split --
exact, instant, auditable -- and the model independently reads the prose
and says whether the numbers match the stated intent.

A checker is allowed to be imperfect in a way an allocator is not. A
missed fault costs what we already have today (nothing catches it); a
false alarm costs one glance from the user. So the two error types are
measured separately, and the false-alarm rate on correct splits is the
number that decides whether this is usable.
"""
from __future__ import annotations

import pathlib
import argparse, json, sys, time
import urllib.request as u

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from scenarios import CATEGORIES, ORDER, SCENARIOS, engine_split

OLLAMA = "http://192.168.86.196:11434/api/chat"

SCHEMA = {
    "type": "object",
    "properties": {
        "matches_intent": {"type": "boolean"},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "what_is_wrong": {"type": "string"},
                },
                "required": ["category", "what_is_wrong"],
            },
        },
    },
    "required": ["matches_intent", "problems"],
}

PROMPT = """Check whether a paycheck was split the way the budget says it
should be.

The paycheck was {gross} ({gross_cents} cents).

Each category below says in its own words how much it should get, shows
what it held BEFORE the split, and what it was actually GIVEN.

{cats}

Given total: {total_cents} cents.

Decide whether the given amounts match what the categories say. Check:
- does each amount match what that category's words ask for?
- do the amounts add up to exactly {gross_cents} cents?
- was anything funded that should not have been, or skipped that should
  not have been?

Remember: categories are funded top to bottom, so if the money ran out a
lower category legitimately gets less than it asked for, or nothing.

Set matches_intent true if it is all correct. If not, list only the
categories that are actually wrong. Return JSON only."""


def fmt(c: int) -> str:
    return f"${c/100:,.2f}"


def build(gross_cents: int, balances: dict, alloc: dict) -> str:
    lines = []
    for i, c in enumerate(CATEGORIES, 1):
        k = c["key"]
        lines.append(
            f"{i}. {c['name']}\n"
            f"   says: {c['description']}\n"
            f"   held before: {fmt(balances[k])} ({balances[k]} cents)\n"
            f"   GIVEN: {fmt(alloc[k])} ({alloc[k]} cents)"
        )
    return PROMPT.format(gross=fmt(gross_cents), gross_cents=gross_cents,
                         cats="\n".join(lines), total_cents=sum(alloc.values()))


def ask(model: str, prompt: str) -> tuple[dict | None, float, str]:
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "stream": False, "format": SCHEMA,
                       "options": {"temperature": 0}}).encode()
    req = u.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with u.urlopen(req, timeout=900) as r:
            data = json.loads(r.read())
    except Exception as e:
        return None, time.time() - t0, f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    content = data.get("message", {}).get("content", "")
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    try:
        return json.loads(content), dt, ""
    except Exception as e:
        return None, dt, f"unparseable: {e}"


def faults(truth: dict, gross: int) -> list[tuple[str, dict, str]]:
    """Plausible ways a split could be wrong, not absurd ones.

    Each is something a buggy rule or a bad AI allocation would actually
    produce, so detecting them is worth something.
    """
    out = []
    sink = ORDER[-1]

    # Money vanishes: the remainder was never computed. This is exactly
    # what qwen2.5vl did in every single arm-B run.
    if truth[sink] > 0:
        d = dict(truth); d[sink] = 0
        out.append(("dropped_remainder", d,
                    f"{sink} zeroed, {fmt(truth[sink])} vanishes"))

    # A conditional fires when it should not.
    if truth["carins"] == 0:
        d = dict(truth); d["carins"] = 5000; d[sink] = max(0, d[sink] - 5000)
        out.append(("conditional_fired_wrongly", d,
                    "car insurance funded despite being over its threshold"))

    # A refill overshoots its target.
    d = dict(truth); d["food"] = truth["food"] + 10000
    d[sink] = max(0, d[sink] - 10000)
    if d != truth:
        out.append(("refill_overshoot", d, "Food overfunded by $100"))

    # Priority inverted: savings paid before rent.
    if truth["rent"] > 0 and truth["rent"] >= 20000:
        d = dict(truth); d["rent"] = truth["rent"] - 20000
        d["savings"] = truth["savings"] + 20000
        out.append(("priority_inverted", d, "$200 moved from Rent to Savings"))

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3:14b")
    a = ap.parse_args()

    print(f"model={a.model}\n")
    clean_ok = clean_n = 0
    caught = missed = 0
    # A request that never returned is NOT the model passing or failing a
    # judgement, and counting it as either makes the summary a lie. The
    # first run of this reported "4/8 false alarms" when the truth was
    # "4 answered, 1 timed out, 3 were HTTP 500 from a host whose disk had
    # failed".
    errors: list[str] = []
    secs = []
    print("  CORRECT splits -- the model should pass these")
    print("  " + "-" * 72)
    for s in SCENARIOS:
        truth = engine_split(s["gross_cents"], s["balances"])
        raw, dt, err = ask(a.model, build(s["gross_cents"], s["balances"], truth))
        secs.append(dt)
        clean_n += 1
        if raw is None:
            errors.append(f"clean/{s['id']}: {err[:60]}")
            clean_n -= 1          # not a judgement, so not a denominator
            print(f"  {s['id']:22} ERROR  {err[:50]}")
            continue
        ok = bool(raw.get("matches_intent"))
        clean_ok += ok
        note = "" if ok else "  flagged: " + "; ".join(
            f"{p.get('category')}: {p.get('what_is_wrong','')[:40]}"
            for p in raw.get("problems", [])[:2])
        print(f"  {s['id']:22} {dt:5.1f}s  {'pass' if ok else 'FALSE ALARM'}{note}")

    print(f"\n  INJECTED faults -- the model should catch these")
    print("  " + "-" * 72)
    for s in SCENARIOS:
        truth = engine_split(s["gross_cents"], s["balances"])
        for name, broken, desc in faults(truth, s["gross_cents"]):
            raw, dt, err = ask(a.model, build(s["gross_cents"], s["balances"], broken))
            secs.append(dt)
            if raw is None:
                errors.append(f"fault/{s['id']}/{name}: {err[:50]}")
                print(f"  {s['id']:16}{name:26} ERROR   {err[:30]}")
                continue
            flagged = not raw.get("matches_intent")
            caught += flagged
            missed += not flagged
            print(f"  {s['id']:16}{name:26} {'CAUGHT' if flagged else 'missed '}  {desc[:34]}")

    print()
    if errors:
        # Loud, and before the scores, so a broken run cannot be mistaken
        # for a measured one.
        print(f"  !! {len(errors)} request(s) never returned — "
              f"the scores below cover only what did")
        for e in errors[:6]:
            print(f"     {e}")
        if len(errors) > 6:
            print(f"     ... and {len(errors)-6} more")
        print()
    if clean_n > 0:
        print(f"  false alarms on correct splits: {clean_n-clean_ok}/{clean_n}")
    else:
        print(f"  false alarms on correct splits: no valid runs")
    if caught + missed > 0:
        print(f"  faults caught:                  {caught}/{caught+missed}")
    else:
        print(f"  faults caught:                  no valid runs")
    if secs:
        print(f"  mean latency:                   {sum(secs)/len(secs):.1f}s")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
