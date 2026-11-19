#!/usr/bin/env python3
"""Compare vision models reading a real receipt photograph.

Ground truth is the paper slip in the table below, transcribed by hand.
The photo itself is not committed (it carries a card number); point
--image at your own, or at the one in the repo root if it is still
there.

Usage:
    python3 experiments/receipt-vision/run.py [--runs 3] [--image PATH] [model ...]
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import time
import urllib.request

DECK = "http://your-ai-host:11434"
DEFAULT_MODELS = ["qwen2.5vl:7b", "gemma3:12b"]

# The paper, read by eye.
TRUTH_ITEMS = 8
TRUTH_SUM = 2160
TRUTH_SUBTOTAL, TRUTH_TAX, TRUTH_TOTAL = 2160, 65, 2225

NON_ITEM = ("SUBTOTAL", "TAX", "TOTAL", "TEND", "CHANGE", "VISA", "CASH")

SCHEMA = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string"},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "description": {"type": "string", "description": "the line as printed"},
            "amount_cents": {"type": "integer",
                             "description": "line total in CENTS. $4.99 is 499."},
            "quantity": {"type": "integer"}},
            "required": ["description", "amount_cents"]}},
        "subtotal_cents": {"type": "integer"},
        "tax_cents": {"type": "integer"},
        "tip_cents": {"type": "integer"},
        "total_cents": {"type": "integer"}},
    "required": ["merchant", "items", "subtotal_cents", "tax_cents",
                 "tip_cents", "total_cents"],
}


def _prompt() -> str:
    """The shipped prompt, imported so this cannot drift from production."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "backend/app/ai/tasks.py").read_text()
    head = 'RECEIPT_PROMPT = """'
    body = src[src.index(head) + len(head):]
    return body[:body.index('"""')]


def ask(model: str, b64: str, temperature: float = 0.0) -> tuple[dict | None, float]:
    # Pinned, always. Unset does NOT mean zero -- ollama falls back to
    # the model's Modelfile, and qwen2.5vl ships 0.0001 while gemma3
    # ships 1. The first run of this experiment did not pin it and
    # measured those defaults instead of the models.
    payload = {"model": model, "stream": False, "format": SCHEMA,
               "options": {"temperature": temperature},
               "messages": [{"role": "user", "content": _prompt(), "images": [b64]}]}
    req = urllib.request.Request(
        f"{DECK}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    started = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        body = json.loads(r.read())
    try:
        return json.loads(body["message"]["content"]), time.time() - started
    except Exception:
        return None, time.time() - started


def score(parsed: dict) -> dict:
    items = parsed.get("items") or []
    real = [i for i in items
            if not any(w in str(i.get("description", "")).upper() for w in NON_ITEM)]
    return {
        "items": len(real),
        "sum": sum(i.get("amount_cents") or 0 for i in real),
        "junk": len(items) - len(real),
        "subtotal": parsed.get("subtotal_cents"),
        "tax": parsed.get("tax_cents"),
        "total": parsed.get("total_cents"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="*", default=None)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--image", default=None)
    ap.add_argument("--temperature", type=float, default=0.0)
    args = ap.parse_args()

    image = args.image
    if not image:
        root = pathlib.Path(__file__).resolve().parents[2]
        found = sorted(root.glob("*.jpg"))
        if not found:
            print("no image: pass --image PATH (receipt photos are gitignored)")
            return 2
        image = str(found[0])
    b64 = base64.b64encode(pathlib.Path(image).read_bytes()).decode()
    print(f"image: {image}  ({len(b64) // 1024} KB base64)")
    print(f"truth: {TRUTH_ITEMS} items, sum {TRUTH_SUM}, "
          f"{TRUTH_SUBTOTAL}/{TRUTH_TAX}/{TRUTH_TOTAL}")
    print(f"temperature: {args.temperature}\n")

    for model in (args.models or DEFAULT_MODELS):
        for n in range(1, args.runs + 1):
            try:
                parsed, secs = ask(model, b64, args.temperature)
            except Exception as exc:  # noqa: BLE001
                print(f"  {model:14} run{n}: FAILED {exc}")
                continue
            if parsed is None:
                print(f"  {model:14} run{n}: UNPARSEABLE ({secs:.1f}s)")
                continue
            s = score(parsed)
            ok = "SUM-OK" if s["sum"] == TRUTH_SUM else "sum-wrong"
            print(f"  {model:14} run{n}: {s['items']} items  "
                  f"sum ${s['sum'] / 100:>8,.2f}  "
                  f"sub={s['subtotal']} tax={s['tax']} tot={s['total']}  "
                  f"junk={s['junk']}  {secs:.1f}s  {ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
