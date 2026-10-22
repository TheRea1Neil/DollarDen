"""Can the model pick the right category for a charge?

A different question from ADR 0006. That tested the model DIVIDING a
paycheck -- arithmetic under a global conservation constraint, which it
could not do. This is classification: pick one label from a list. No
sums, nothing to conserve.

Scored for the job it would actually do. Auto-filing is a precision
problem: a wrong answer is money in the wrong envelope that nobody
notices, while declining costs one tap. So "wrong" and "declined" are
counted separately and never averaged together.
"""
from __future__ import annotations

import argparse, json, pathlib, sys, time
import urllib.request as u

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from fixtures import CASES, NAMES, categories

OLLAMA = "http://192.168.86.196:11434/api/chat"

UNSURE = "UNSURE"

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": NAMES + [UNSURE]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "why": {"type": "string", "description": "one short sentence"},
    },
    "required": ["category", "confidence", "why"],
}

PROMPT = """Which budget category should this charge come out of?

The charge:
  {merchant}
  {amount}

The categories:
{cats}

Pick the ONE category this charge belongs to.

If the merchant name does not tell you what was actually bought — a bare
payment processor, a transfer, a generic terminal string — answer
"{unsure}". A wrong guess puts money in the wrong envelope and is worse
than asking. "{unsure}" is a good answer when the name is genuinely
uninformative.

Return JSON only."""


def render(c: dict) -> str:
    line = f"  - {c['name']} ({c['kind']})"
    if c["description"]:
        line += f": {c['description']}"
    return line


def ask(model: str, prompt: str) -> tuple[dict | None, float, str]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "format": SCHEMA,
        "options": {"temperature": 0},
    }).encode()
    req = u.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with u.urlopen(req, timeout=300) as r:
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5vl:7b")
    ap.add_argument("--arm", default="nuanced",
                    choices=["plain", "generic", "nuanced"])
    a = ap.parse_args()

    cats = "\n".join(render(c) for c in categories(a.arm))
    print(f"model={a.model}  arm={a.arm}\n")

    tally = {d: {"right": 0, "wrong": 0, "declined": 0, "n": 0}
             for d in ("clear", "needs_desc", "conflict", "ambiguous")}
    secs, wrongs, errors = [], [], []

    print(f"  {'merchant':28}{'picked':<20}{'verdict':<10} conf")
    print("  " + "-" * 74)
    for merchant, cents, accepted, difficulty in CASES:
        prompt = PROMPT.format(merchant=merchant, amount=f"${cents/100:,.2f}",
                               cats=cats, unsure=UNSURE)
        raw, dt, err = ask(a.model, prompt)
        secs.append(dt)
        t = tally[difficulty]
        t["n"] += 1
        if raw is None:
            # NOT a decline: the model never answered. Counting an
            # outage as "correctly declined" would flatter the score.
            t["n"] -= 1
            errors.append(f"{merchant}: {err[:50]}")
            print(f"  {merchant:28}{'(error)':<20}{'-':<10} {err[:24]}")
            continue

        picked = raw.get("category", UNSURE)
        conf = raw.get("confidence", "?")

        if difficulty == "ambiguous":
            # Declining is the right answer here.
            good = picked == UNSURE
            verdict = "right" if good else "GUESSED"
        else:
            if picked == UNSURE:
                verdict = "declined"
            else:
                good = picked in accepted
                verdict = "right" if good else "WRONG"

        if verdict == "right":
            t["right"] += 1
        elif verdict == "declined":
            t["declined"] += 1
        else:
            t["wrong"] += 1
            wrongs.append((merchant, picked, accepted, raw.get("why", "")))

        print(f"  {merchant:28}{picked:<20}{verdict:<10} {conf}")

    print()
    if errors:
        print(f"  !! {len(errors)} request(s) never answered — "
              f"the scores below cover only what did")
        for e in errors[:4]:
            print(f"     {e}")
        print()
    for d in ("clear", "needs_desc", "conflict", "ambiguous"):
        t = tally[d]
        if not t["n"]:
            continue
        what = "declined (correct)" if d == "ambiguous" else "right"
        print(f"  {d:12} {t['right']}/{t['n']} {what}, "
              f"{t['wrong']} wrong, {t['declined']} declined")

    total_wrong = sum(t["wrong"] for t in tally.values())
    answered = sum(t["right"] + t["wrong"] for t in tally.values())
    print(f"\n  wrong answers overall: {total_wrong}")
    if answered:
        print(f"  precision when it answers: "
              f"{100 * (answered - total_wrong) / answered:.0f}%")
    answered_secs = [x for x in secs if x > 0.1]
    if answered_secs:
        print(f"  median latency: "
              f"{sorted(answered_secs)[len(answered_secs)//2]:.1f}s")
    if errors:
        raise SystemExit(1)

    if wrongs:
        print("\n  what it got wrong:")
        for m, picked, accepted, why in wrongs:
            want = " or ".join(accepted) if accepted else "UNSURE"
            print(f"    {m:26} -> {picked:20} (wanted {want})")
            if why:
                print(f"      \"{why[:80]}\"")


if __name__ == "__main__":
    main()
