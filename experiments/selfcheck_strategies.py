"""Does an agentic self-check improve proposal quality? Measure it.

Three strategies, same prompt, same model:

  A  single pass            what we ship today
  B  self-critique          model reviews its own output and rewrites
  C  validator feedback     our deterministic validator's complaints are
                            fed back, up to 2 retries

C is the interesting one. B asks the model to grade itself, which is only
as good as its own judgement. C hands it objective, specific errors
produced by code that cannot be fooled.

Scored on:
  applicable    passes validation with zero blocking problems
  problems      count of blocking problems
  magnitude     did it get a known target right (the 10x cents bug)
  seconds       wall clock
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time

from app.ai.provider import OllamaProvider
from app.ai.tasks import DESIGN_PROMPT, DESIGN_SCHEMA, _validate_design
from app.core.config import get_settings


class FakeCat:
    def __init__(self, name, kind, target=None):
        self.name, self.kind, self.target_cents = name, kind, target


# Deliberately harder than the shipped case: nothing to copy from, several
# numeric constraints to get right at once, and a conditional rule -- the
# most intricate type. An easy task hides any difference between
# strategies, which is exactly what the first run of this experiment did.
EXISTING: list = []
GOALS = (
    "Save $6,000 for an emergency fund. Then save for a newer car. "
    "Stop eating out so much. Always pay tithing first, before anything else."
)
INSTRUCTIONS = (
    "Paid every two weeks. Rent is $350 a paycheck. Health insurance is $150. "
    "Keep groceries topped up to $300 each paycheck. "
    "Keep short-term savings topped up to $2,000. "
    "Only add $50 to car insurance when its balance is under $450. "
    "Tithing is 10 percent of gross."
)

# Every one of these is stated verbatim in INSTRUCTIONS, so reproducing them
# is a fact question rather than a judgement call. They are checked in RULE
# PARAMS, which is where the dollars/cents error actually appears -- the
# first version of this experiment checked category.target_cents, a field
# the model never populates, and so measured nothing.
EXPECTED_PARAM = {
    "rent": ("amount_cents", 35000),
    "health": ("amount_cents", 15000),
    "grocer": ("target_cents", 30000),
    "short-term": ("target_cents", 200000),
    "tithing": ("rate", "0.10"),
}


def build_prompt() -> str:
    lines = "\n".join(
        f"  - {c.name} ({c.kind})"
        + (f", target ${c.target_cents / 100:.2f}" if c.target_cents else "")
        for c in EXISTING
    )
    return DESIGN_PROMPT.format(
        existing_block=(
            "The user ALREADY has these categories. Reuse them by name where they "
            f"fit, and only add what is missing:\n{lines}"
        ),
        goals=GOALS,
        request_block=f"Additional instructions:\n{INSTRUCTIONS}",
    )


def score(result: dict) -> dict:
    """Objective scoring against values stated verbatim in the prompt."""
    correct = 0
    checked = 0
    wrong: list[str] = []

    for rule in result.get("split_rules", []):
        name = rule.get("category_name", "").lower()
        params = rule.get("params") or {}
        for needle, (key, want) in EXPECTED_PARAM.items():
            if needle not in name:
                continue
            checked += 1
            got = params.get(key)
            if key == "rate":
                ok = got is not None and abs(float(got) - float(want)) < 1e-6
            else:
                ok = got == want
            if ok:
                correct += 1
            else:
                wrong.append(f"{rule.get('category_name')}.{key}={got!r} want {want!r}")
            break

    return {
        "applicable": bool(result.get("applicable")),
        "problems": len(result.get("problems", [])),
        "facts_checked": checked,
        "facts_correct": correct,
        "wrong": wrong,
        "rules": len(result.get("split_rules", [])),
    }


async def strategy_a(provider, prompt):
    res = await provider.chat(prompt, schema=DESIGN_SCHEMA)
    return _validate_design(json.loads(res.content), EXISTING), 1


async def strategy_b(provider, prompt):
    """Self-critique: the model grades and rewrites its own answer."""
    first = await provider.chat(prompt, schema=DESIGN_SCHEMA)
    critique_prompt = (
        f"{prompt}\n\n"
        f"You produced this draft:\n{first.content}\n\n"
        "Review it critically for mistakes before finalising:\n"
        "- Is every params object filled correctly for its rule type?\n"
        "- Is there EXACTLY ONE remainder rule, and is it last?\n"
        "- Are all amounts in CENTS, not dollars? ($2,000 is 200000)\n"
        "- Do targets match any values stated above?\n\n"
        "Return the corrected JSON only."
    )
    second = await provider.chat(critique_prompt, schema=DESIGN_SCHEMA)
    return _validate_design(json.loads(second.content), EXISTING), 2


async def strategy_c(provider, prompt, max_retries=2):
    """Validator feedback: objective errors handed back for repair."""
    current = prompt
    calls = 0
    result = None
    for _ in range(max_retries + 1):
        res = await provider.chat(current, schema=DESIGN_SCHEMA)
        calls += 1
        result = _validate_design(json.loads(res.content), EXISTING)
        if result["applicable"]:
            return result, calls
        complaints = "\n".join(f"  - {p}" for p in result["problems"])
        current = (
            f"{prompt}\n\n"
            f"You produced this draft:\n{res.content}\n\n"
            f"A validator rejected it with these SPECIFIC errors:\n{complaints}\n\n"
            "Fix exactly these errors and return the corrected JSON only."
        )
    return result, calls


async def main(trials: int):
    settings = get_settings()
    provider = OllamaProvider(settings.ai_deck_url, settings)
    prompt = build_prompt()

    strategies = {
        "A single-pass": strategy_a,
        "B self-critique": strategy_b,
        "C validator-loop": strategy_c,
    }
    results: dict[str, list] = {k: [] for k in strategies}

    for name, fn in strategies.items():
        print(f"\n{name}", flush=True)
        for i in range(trials):
            t0 = time.time()
            try:
                result, calls = await fn(provider, prompt)
                s = score(result)
                s["seconds"] = time.time() - t0
                s["calls"] = calls
                results[name].append(s)
                print(f"  {i+1:2}/{trials}  applicable={str(s['applicable']):<5} "
                      f"problems={s['problems']}  "
                      f"facts={s['facts_correct']}/{s['facts_checked']}  "
                      f"rules={s['rules']}  {s['seconds']:5.1f}s  calls={calls}",
                      flush=True)
                for w in s["wrong"][:3]:
                    print(f"            wrong: {w}", flush=True)
            except Exception as exc:
                print(f"  {i+1:2}/{trials}  ERROR {type(exc).__name__}: {exc}", flush=True)

    print("\n" + "=" * 72)
    print(f"{'strategy':<18} {'applicable':>11} {'avg problems':>13} "
          f"{'facts right':>13} {'avg sec':>8}")
    print("-" * 72)
    for name, rows in results.items():
        if not rows:
            continue
        ok = sum(r["applicable"] for r in rows)
        probs = statistics.mean(r["problems"] for r in rows)
        fc = sum(r["facts_correct"] for r in rows)
        ft = sum(r["facts_checked"] for r in rows)
        pct = f"{fc}/{ft} ({100 * fc / ft:.0f}%)" if ft else "n/a"
        secs = statistics.mean(r["seconds"] for r in rows)
        print(f"{name:<18} {ok:>6}/{len(rows):<4} {probs:>13.1f} {pct:>13} {secs:>8.1f}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 6))
