"""Three ways to split a paycheck, scored against the same ground truth.

  A  rules engine      AI authors JSON rules once; a deterministic engine
                       executes them. What we ship today.
  B  pure AI           The model reads prose descriptions and the current
                       balances and names the dollar amounts itself. No
                       engine. This is the alternative being asked about.
  C  AI + repair       Arm B, then a deterministic pass that conserves the
                       paycheck exactly and sweeps the residual to the
                       stated catch-all.

Arm A is ground truth by construction, which is the point: it provably
conserves money and provably honours order. The question is how close B
gets, and whether C closes the gap.
"""
from __future__ import annotations

import pathlib

import argparse, json, statistics, sys, time
import urllib.request as u

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from scenarios import CATEGORIES, ORDER, SCENARIOS, engine_split

OLLAMA = "http://192.168.86.196:11434/api/chat"

# Arm B+ : the same task, but the model may show its work first.
# Without this the comparison conflates "allocating directly is hard" with
# "a non-thinking 7B model given no scratchpad is bad at arithmetic".
SCRATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "working": {
            "type": "string",
            "description": "Work through it step by step before answering: "
                           "for each category in order, what it should get "
                           "and how much of the paycheck is left afterward. "
                           "Then check your amounts sum to the paycheck.",
        },
        "allocations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string",
                                 "enum": [c["name"] for c in CATEGORIES]},
                    "amount_cents": {"type": "integer"},
                },
                "required": ["category", "amount_cents"],
            },
        },
    },
    "required": ["working", "allocations"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "allocations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string",
                                 "enum": [c["name"] for c in CATEGORIES]},
                    "amount_cents": {"type": "integer"},
                },
                "required": ["category", "amount_cents"],
            },
        },
    },
    "required": ["allocations"],
}

PROMPT = """You are splitting one paycheck into budget categories.

The paycheck is {gross} ({gross_cents} cents).

Split it across these categories, in this order. Each category says in its
own words how much it should get. Fund them strictly top to bottom: a
category only gets money if money is still left after the ones above it.

{cats}

Rules you must follow:
- Work in INTEGER CENTS. {gross_cents} cents is the whole paycheck.
- The amounts you give MUST add up to EXACTLY {gross_cents} cents. Not more,
  not less.
- Fund in the listed order. If the money runs out partway down, the
  categories above get what they asked for and the ones below get 0. A
  category may get a PARTIAL amount if only some money is left when its
  turn comes.
- "Top up to $X" means add only the shortfall: if it already holds $X or
  more, add 0.
- Never give a negative amount.

Return JSON only."""


def fmt(cents: int) -> str:
    return f"${cents/100:,.2f}"


def build_prompt(gross_cents: int, balances: dict[str, int]) -> str:
    lines = []
    for i, c in enumerate(CATEGORIES, 1):
        lines.append(
            f"{i}. {c['name']} — currently holds "
            f"{fmt(balances[c['key']])} ({balances[c['key']]} cents)\n"
            f"   {c['description']}"
        )
    return PROMPT.format(gross=fmt(gross_cents), gross_cents=gross_cents,
                         cats="\n".join(lines))


def ask(model: str, prompt: str, timeout: int = 600,
        scratchpad: bool = False) -> tuple[dict | None, float, str]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": SCRATCH_SCHEMA if scratchpad else SCHEMA,
        # Greedy, so any run-to-run variation is the model's own and not
        # sampling noise we introduced.
        "options": {"temperature": 0},
    }).encode()
    req = u.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with u.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception as e:
        return None, time.time() - t0, f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    content = data.get("message", {}).get("content", "")
    # qwen3 is a thinking model; strip any reasoning block before parsing.
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    try:
        return json.loads(content), dt, ""
    except Exception as e:
        return None, dt, f"unparseable: {e}: {content[:200]}"


BY_NAME = {c["name"]: c["key"] for c in CATEGORIES}


def to_dict(raw: dict) -> dict[str, int]:
    out = {k: 0 for k in ORDER}
    for a in raw.get("allocations", []):
        key = BY_NAME.get(a.get("category"))
        if key is not None:
            out[key] = int(a.get("amount_cents") or 0)
    return out


def repair(alloc: dict[str, int], gross_cents: int) -> dict[str, int]:
    """Arm C: make the model's answer safe without second-guessing it.

    Deterministic and unambiguous only -- no negatives, no spending money
    that does not exist, and the residual lands in the stated catch-all.
    This cannot fix a wrong judgement, only an unsafe one.
    """
    out = {k: max(0, v) for k, v in alloc.items()}
    sink = ORDER[-1]  # Car Fund, the declared remainder

    # Never allocate more than the paycheck: trim from the bottom up,
    # which is the same priority the plan states.
    over = sum(out.values()) - gross_cents
    if over > 0:
        for k in reversed(ORDER):
            if over <= 0:
                break
            take = min(out[k], over)
            out[k] -= take
            over -= take
    # Anything unallocated goes to the catch-all, as the plan says.
    short = gross_cents - sum(out.values())
    if short > 0:
        out[sink] += short
    return out


def score(got: dict[str, int], truth: dict[str, int], gross_cents: int) -> dict:
    total = sum(got.values())
    per_cat = {k: got[k] - truth[k] for k in ORDER}
    wrong = [k for k in ORDER if got[k] != truth[k]]

    # A priority inversion: something lower in the order got funded while
    # something above it was left short of what the engine gave it. This is
    # the failure that actually hurts -- rent losing to savings.
    inversions = []
    for i, hi in enumerate(ORDER):
        if got[hi] < truth[hi]:
            for lo in ORDER[i + 1:]:
                if got[lo] > truth[lo]:
                    inversions.append(f"{lo}>{hi}")
    return dict(
        exact=not wrong,
        wrong=wrong,
        conservation_error=total - gross_cents,
        conserved=total == gross_cents,
        mae_cents=sum(abs(v) for v in per_cat.values()) // len(ORDER),
        max_err_cents=max(abs(v) for v in per_cat.values()),
        inversions=sorted(set(inversions)),
        negatives=[k for k in ORDER if got[k] < 0],
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5vl:7b")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", default=None)
    ap.add_argument("--scratchpad", action="store_true",
                    help="arm B+: let the model show its working first")
    a = ap.parse_args()

    print(f"model={a.model}  repeats={a.repeats}  scenarios={len(SCENARIOS)}"
          f"{'  [scratchpad]' if a.scratchpad else ''}\n")
    rows = []
    for s in SCENARIOS:
        truth = engine_split(s["gross_cents"], s["balances"])
        prompt = build_prompt(s["gross_cents"], s["balances"])
        runs = []
        for r in range(a.repeats):
            raw, dt, err = ask(a.model, prompt, scratchpad=a.scratchpad)
            if raw is None:
                runs.append(dict(ok=False, err=err, secs=dt))
                print(f"  {s['id']:20} run{r+1}  FAILED  {err[:70]}")
                continue
            b = to_dict(raw)
            c = repair(b, s["gross_cents"])
            sb, sc = score(b, truth, s["gross_cents"]), score(c, truth, s["gross_cents"])
            runs.append(dict(ok=True, secs=dt, b=b, c=c, score_b=sb, score_c=sc))
            flag = "exact" if sb["exact"] else (
                f"off by {sb['max_err_cents']/100:,.2f} max"
                + (f", {sb['conservation_error']/100:+,.2f} total"
                   if not sb["conserved"] else ""))
            print(f"  {s['id']:20} run{r+1}  {dt:5.1f}s  B: {flag}"
                  + (f"  -> C: exact" if sc["exact"] and not sb["exact"] else ""))
        rows.append(dict(scenario=s, truth=truth, runs=runs))

    out = a.out or (f"results-{a.model.replace(':','-')}"
                    f"{'-scratch' if a.scratchpad else ''}.json")
    with open(out, "w") as f:
        json.dump(dict(model=a.model, repeats=a.repeats, rows=rows), f, indent=1)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
