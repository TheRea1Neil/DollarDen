"""Arm D: descriptions as the spec, COMPILED to rules once.

Neil's idea is that a category's description should define how it gets
funded. Arm B does that by asking the model to allocate every payday.
Arm D does it by asking the model to translate the prose into split rules
ONCE; the deterministic engine then runs those rules every payday forever.

Same prose in. The difference is where the model sits: inside the monthly
money path, or at the edge of it, compiling a spec a human can read and
approve before it ever touches a paycheck.
"""
from __future__ import annotations

import pathlib

import argparse, json, sys, time
import urllib.request as u

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "backend"))

from app.budget.split import RuleType, SplitRule, SplitPlanError, apply_split
from scenarios import CATEGORIES, ORDER, SCENARIOS, engine_split
from run import score

OLLAMA = "http://192.168.86.196:11434/api/chat"

RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string",
                                 "enum": [c["name"] for c in CATEGORIES]},
                    "order_index": {"type": "integer"},
                    "rule_type": {"type": "string",
                                  "enum": ["percent", "fixed", "refill_to",
                                           "conditional", "remainder"]},
                    "rate": {"type": "string",
                             "description": "percent rules only, '0.10' = 10%"},
                    "of": {"type": "string", "enum": ["gross", "remaining"],
                           "description": "percent rules only"},
                    "amount_cents": {"type": "integer",
                                     "description": "fixed rules only"},
                    "target_cents": {"type": "integer",
                                     "description": "refill_to rules only"},
                    "when_value_cents": {
                        "type": "integer",
                        "description": "conditional only: the threshold in CENTS, "
                                       "e.g. 45000 for $450",
                    },
                    "when_op": {"type": "string", "enum": ["<", "<=", ">", ">=", "=="],
                                "description": "conditional only"},
                    "then_amount_cents": {"type": "integer",
                                          "description": "conditional only: amount when it fires"},
                },
                # Every field a rule type can need is required. Leaving
                # `when_value_cents` optional is what made both models omit
                # the threshold -- an operator with nothing to compare to.
                "required": ["category", "order_index", "rule_type",
                             "when_op", "when_value_cents", "then_amount_cents",
                             "rate", "of", "amount_cents", "target_cents"],
            },
        },
    },
    "required": ["rules"],
}

PROMPT = """Translate a budget written in plain English into funding rules.

These are the categories, in the order they should be funded. Each says in
its own words how much of a paycheck it should get.

{cats}

Produce one rule per category, with order_index 1..{n} matching the order
above. Pick the rule_type that matches what the words say:

  percent      a share of pay. Set rate ("0.10" means 10%) and of="gross".
  fixed        the same amount every time. Set amount_cents.
  refill_to    topped back up to a target, adding only the shortfall.
               Set target_cents.
  conditional  funded only while some threshold holds. Set when_op,
               when_value_cents, and then_amount_cents.
  remainder    whatever is left over. Exactly one rule, and it must be last.

All money is INTEGER CENTS: $350.00 is 35000, $2,000.00 is 200000.

Return JSON only."""


def build_prompt() -> str:
    lines = [f"{i}. {c['name']}\n   {c['description']}"
             for i, c in enumerate(CATEGORIES, 1)]
    return PROMPT.format(cats="\n".join(lines), n=len(CATEGORIES))


BY_NAME = {c["name"]: c["key"] for c in CATEGORIES}


def to_engine_rules(raw: dict) -> list[SplitRule]:
    out = []
    for r in sorted(raw.get("rules", []), key=lambda x: x.get("order_index", 999)):
        key = BY_NAME.get(r.get("category"))
        if key is None:
            continue
        rt = r.get("rule_type")
        params: dict = {}
        if rt == "percent":
            params = {"rate": str(r.get("rate", "0")), "of": r.get("of", "gross")}
        elif rt == "fixed":
            params = {"amount_cents": int(r.get("amount_cents") or 0)}
        elif rt == "refill_to":
            params = {"target_cents": int(r.get("target_cents") or 0)}
        elif rt == "conditional":
            # No silent defaults. A missing threshold used to become 0,
            # which turned "only below $450" into a different rule
            # entirely and hid the real failure.
            params = {
                "when": {"field": "balance", "op": r.get("when_op"),
                         "value_cents": r.get("when_value_cents")},
                "then": {"rule_type": "fixed",
                         "params": {"amount_cents": r.get("then_amount_cents")}},
            }
        out.append(SplitRule(category_id=key, order_index=len(out) + 1,
                             rule_type=RuleType(rt), params=params, enabled=True))
    return out


def run_rules(rules: list[SplitRule], gross_cents: int,
              balances: dict[str, int]) -> dict[str, int] | str:
    try:
        res = apply_split(gross_cents, rules, dict(balances))
    except (SplitPlanError, KeyError, TypeError, ValueError) as e:
        return f"{type(e).__name__}: {e}"
    out = {k: 0 for k in ORDER}
    for a in res.allocations:
        out[a.category_id] = out.get(a.category_id, 0) + a.amount_cents
    return out


def ask(model: str, prompt: str) -> tuple[dict | None, float, str]:
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "stream": False, "format": RULE_SCHEMA,
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
        return None, dt, f"unparseable: {e}: {content[:200]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5vl:7b")
    ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args()

    prompt = build_prompt()
    print(f"model={a.model}  compiling prose -> rules, {a.repeats} attempts\n")
    attempts = []
    for r in range(a.repeats):
        raw, dt, err = ask(a.model, prompt)
        if raw is None:
            print(f"  attempt {r+1}: FAILED {err[:90]}")
            attempts.append(dict(ok=False, err=err, secs=dt))
            continue
        rules = to_engine_rules(raw)
        print(f"  attempt {r+1}: {dt:5.1f}s  " +
              ", ".join(f"{x.category_id}:{x.rule_type.value}" for x in rules))

        results, scores = {}, []
        for s in SCENARIOS:
            truth = engine_split(s["gross_cents"], s["balances"])
            got = run_rules(rules, s["gross_cents"], s["balances"])
            if isinstance(got, str):
                results[s["id"]] = dict(error=got)
                scores.append(None)
                continue
            sc = score(got, truth, s["gross_cents"])
            results[s["id"]] = dict(got=got, score=sc)
            scores.append(sc)
        ok = [x for x in scores if x]
        exact = sum(1 for x in ok if x["exact"])
        cons = sum(1 for x in ok if x["conserved"])
        print(f"            -> exact {exact}/{len(SCENARIOS)}, "
              f"conserved {cons}/{len(SCENARIOS)}"
              + (f", {len(scores)-len(ok)} engine errors" if len(ok) < len(scores) else ""))
        for sid, v in results.items():
            if "error" in v:
                print(f"               {sid}: ENGINE ERROR {v['error'][:60]}")
            elif not v["score"]["exact"]:
                print(f"               {sid}: wrong on {','.join(v['score']['wrong'])}"
                      f" (max {v['score']['max_err_cents']/100:,.2f})")
        attempts.append(dict(ok=True, secs=dt, raw=raw, results=results))

    with open(f"armd-{a.model.replace(':','-')}.json", "w") as f:
        json.dump(dict(model=a.model, attempts=attempts), f, indent=1)


if __name__ == "__main__":
    main()
