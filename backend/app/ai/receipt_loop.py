"""Reading a receipt as a tool loop, with the arithmetic done in Python.

WHY A LOOP AND NOT ONE PROMPT
-----------------------------
Measured on three real receipts, the model reads prices well and cannot
add them up. Asked to do both in one shot it burned 38,000-47,000
characters of reasoning and returned nothing, or returned lines that
summed to $50.08 on a $22.25 receipt. Its own reasoning traces showed
the right digits; the total drifted on the way out.

So it is given no arithmetic to do. Every write to the scratchpad is
summed here and the result handed straight back. With that one change
the reasoning collapsed from ~16,000 characters to zero, the Walmart
receipt went from "never finishes" to exact in 8 seconds, and the Sam's
receipt went from 19/29 item prices correct to 26/29.

THE THREE RULES THAT MATTER
---------------------------
1. The TOTAL IS NOT THE MODEL'S TO CHOOSE. It comes from the bank. Told
   only "make it add up", the model misread tax as $0.60 and then
   reported the total as $22.20 to fit -- internally consistent,
   externally wrong, and nothing on the screen would have shown it.

2. NEVER INVENT AN AMOUNT. Unable to close the Target receipt, it set a
   toy to $15.78 -- a price printed nowhere -- purely to force the sum.
   A leftover difference is acceptable; a fabricated line is not,
   because it files the wrong money against a category silently.

3. ALWAYS BALANCE. Whatever is left unexplained becomes one honest line
   against the dominant category, flagged for review. Perfect extraction
   is not the goal; a transaction that reconciles with the bank is.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

log = logging.getLogger("nnf.ai.receipt_loop")

# Hard ceiling on tool calls. The prompt also asks the model to stop,
# but a model that cannot converge must be stopped rather than trusted
# to stop itself.
#
# Lowered from 14 after a real 39-line grocery receipt spent all 14
# calls and 198 seconds, and still finished with a -$4.72 remainder.
# Every call re-sends the whole conversation INCLUDING the photo, so
# the cost is roughly linear in calls -- and the remainder line exists
# precisely so the model does not have to grind. Six attempts reach the
# same answer far sooner; a seventh has never been what rescued a
# receipt.
MAX_TOOL_CALLS = 7
MAX_FIX_ATTEMPTS = 4

UNACCOUNTED = "Unaccounted (receipt did not add up)"


def to_cents(value: Any) -> int | None:
    """Dollars to integer cents, via Decimal (ADR 0002). None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


@dataclass
class Line:
    description: str
    cents: int
    category: str
    needs_review: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "amount_cents": self.cents,
            "category": self.category,
            "needs_review": self.needs_review,
        }


@dataclass
class Scratchpad:
    """What the model has read so far, plus the arithmetic it cannot do."""

    target_cents: int
    lines: list[Line] = field(default_factory=list)

    @property
    def total_cents(self) -> int:
        return sum(line.cents for line in self.lines)

    @property
    def difference_cents(self) -> int:
        return self.target_cents - self.total_cents

    def summary(self) -> dict[str, Any]:
        d = self.difference_cents
        out = {
            "lines": len(self.lines),
            "sum": _money(self.total_cents),
            "target": _money(self.target_cents),
            "difference": _money(d),
            "matches": d == 0,
        }
        if d:
            out["hint"] = (
                f"your lines are {_money(abs(d))} "
                f"{'under' if d > 0 else 'over'} the target"
            )
        return out

    def save(self, items: Any) -> dict[str, Any]:
        self.lines = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            cents = to_cents(it.get("amount"))
            if cents is None:
                continue
            self.lines.append(Line(
                description=str(it.get("description", "")).strip()[:200],
                cents=cents,
                category=str(it.get("category", "Unsure")).strip() or "Unsure",
            ))
        return self.summary()

    def fix(self, index: Any, amount: Any,
            description: Any = None, category: Any = None) -> dict[str, Any]:
        try:
            i = int(index)
        except (TypeError, ValueError):
            return {"error": "index must be a whole number"}
        if not (0 <= i < len(self.lines)):
            return {"error": f"no line at index {i}; there are {len(self.lines)}"}
        cents = to_cents(amount)
        if cents is None:
            return {"error": "amount must be a number"}
        self.lines[i].cents = cents
        if description:
            self.lines[i].description = str(description).strip()[:200]
        if category:
            self.lines[i].category = str(category).strip()
        return self.summary()

    def reconcile(self) -> Line | None:
        """Absorb any remaining difference into one flagged line.

        Called once, at the end. The dominant category takes it because
        that is where the misread almost certainly was, and because a
        remainder on its own in "Unsure" tells the user less than a
        remainder sitting beside the category it came from.
        """
        gap = self.difference_cents
        if gap == 0:
            return None
        weights: dict[str, int] = {}
        for line in self.lines:
            weights[line.category] = weights.get(line.category, 0) + abs(line.cents)
        dominant = max(weights, key=weights.get) if weights else "Unsure"
        line = Line(UNACCOUNTED, gap, dominant, needs_review=True)
        self.lines.append(line)
        return line


TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "save_items",
        "description": (
            "Write the full list of receipt lines to the scratchpad, replacing "
            "whatever is there. Returns the arithmetic: the sum of your amounts, "
            "the target, and the difference. You never need to add anything up "
            "yourself."),
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "description": "every line on the receipt",
                      "items": {"type": "object", "properties": {
                          "description": {"type": "string"},
                          "amount": {"type": "number",
                                     "description": "dollars; negative for a void or refund"},
                          "category": {"type": "string"}},
                          "required": ["description", "amount", "category"]}}},
            "required": ["items"]}}},
    {"type": "function", "function": {
        "name": "fix_item",
        "description": ("Change one line already on the scratchpad, by index. "
                        "Returns the updated arithmetic."),
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer", "description": "0-based index of the line"},
            "amount": {"type": "number"},
            "description": {"type": "string"},
            "category": {"type": "string"}},
            "required": ["index", "amount"]}}},
    {"type": "function", "function": {
        "name": "submit",
        "description": ("Finish. Succeeds when the scratchpad matches the target; "
                        "otherwise reports the remaining difference."),
        "parameters": {"type": "object", "properties": {}}}},
]


PROMPT = """Read this receipt and record every line on the scratchpad.

This charge was {total}. That figure comes from the bank, so it is certain
and is not something you may change.

Include tax as its own line. A voided or refunded line is printed with a
minus sign and must be recorded as a NEGATIVE amount. Do not record
subtotals, tender, change, card or terminal lines — they are not purchases.

Give every line one of these categories:
{cats}
Use "Unsure" when genuinely unclear.

HOW TO WORK
  1. Read the lines off the image and call save_items with all of them.
  2. The tool replies with the sum and the difference from {total}.
     Never add anything up yourself — the tool does it for you.
  3. If the difference is not zero you have misread one or two lines.
     Look at the image again and call fix_item for each. A common error
     is an extra or missing leading digit.
  4. When the difference is zero, call submit. You are finished.

NEVER INVENT AN AMOUNT
  Only enter a number you can actually read on the receipt. Do not adjust
  a line to an arbitrary value just to make the total come out. A leftover
  difference is recorded separately and is perfectly acceptable; a line
  with a made-up price is not, because it files the wrong amount against a
  category and nobody can tell afterwards.

EXIT CONDITION
  Call submit as soon as difference is 0. If you have tried {max_fix} fixes
  and still do not match, call submit anyway and stop — whatever is left is
  recorded as its own line. An answer that is close and honest beats an
  endless loop, and beats a forced one. At most {max_calls} tool calls."""


@dataclass
class LoopResult:
    lines: list[Line]
    target_cents: int
    tool_calls: int
    closed: bool
    submitted: bool
    remainder: Line | None
    model: str
    seconds: float
    thinking_chars: int

    def as_dict(self) -> dict[str, Any]:
        split: dict[str, int] = {}
        for line in self.lines:
            split[line.category] = split.get(line.category, 0) + line.cents
        return {
            "items": [line.as_dict() for line in self.lines],
            "category_split": [
                {"category": k, "amount_cents": v}
                for k, v in sorted(split.items(), key=lambda kv: -abs(kv[1]))
            ],
            "total_cents": sum(line.cents for line in self.lines),
            "target_cents": self.target_cents,
            "closed_without_remainder": self.closed,
            "remainder_cents": self.remainder.cents if self.remainder else 0,
            "needs_review": [
                i for i, line in enumerate(self.lines) if line.needs_review
            ],
            "tool_calls": self.tool_calls,
            "model": self.model,
            "seconds": round(self.seconds, 1),
        }


async def run_loop(
    provider: Any,
    *,
    image_b64: str,
    target_cents: int,
    categories: list[tuple[str, str]],
    think: bool = False,
    num_ctx: int = 20480,
    num_predict: int = 8192,
) -> LoopResult:
    """Drive the scratchpad loop to a balanced result.

    Returns whatever it got. The caller always receives lines that sum
    to `target_cents`, because `reconcile()` closes any gap with a
    flagged remainder rather than returning a split that disagrees with
    the bank.
    """
    import time

    pad = Scratchpad(target_cents=target_cents)
    prompt = PROMPT.format(
        total=_money(target_cents),
        cats="\n".join(f"  - {n}: {d}" for n, d in categories),
        max_fix=MAX_FIX_ATTEMPTS,
        max_calls=MAX_TOOL_CALLS,
    )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": prompt, "images": [image_b64]}
    ]

    started = time.monotonic()
    calls = 0
    submitted = False
    thinking_chars = 0
    model_name = ""
    # (index, cents) pairs already tried. Repeating one cannot converge:
    # on the Target receipt the model ping-ponged a line between $1.98
    # and $19.98 for eight calls, then invented $15.78 to force a match.
    seen_fixes: set[tuple[Any, int | None]] = set()
    stop = False
    fruitless = 0

    while calls < MAX_TOOL_CALLS and not stop:
        res = await provider.chat(
            "", messages=messages, tools=TOOLS, think=think,
            num_ctx=num_ctx, num_predict=num_predict, temperature=0.0,
        )
        model_name = res.model
        thinking_chars += len(res.thinking or "")

        if not res.tool_calls:
            if res.done_reason == "length":
                log.warning(
                    "receipt loop: model ran out of tokens before acting "
                    "(thinking=%d chars). Raise num_predict.", len(res.thinking or ""))
            break

        messages.append({
            "role": "assistant",
            "content": res.content,
            "tool_calls": [
                {"function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in res.tool_calls
            ],
        })

        for call in res.tool_calls:
            calls += 1
            name = call.get("name")
            args = call.get("arguments") or {}

            if name == "save_items":
                payload = pad.save(args.get("items"))
            elif name == "fix_item":
                key = (args.get("index"), to_cents(args.get("amount")))
                if key in seen_fixes:
                    payload = {
                        "error": "you already tried that exact change and it did "
                                 "not work. Do not guess a different number. Call "
                                 "submit now; the remaining difference is recorded "
                                 "separately."
                    }
                    stop = True
                else:
                    seen_fixes.add(key)
                    before = abs(pad.difference_cents)
                    payload = pad.fix(args.get("index"), args.get("amount"),
                                      args.get("description"), args.get("category"))
                    after = abs(pad.difference_cents)
                    if after >= before and after != 0:
                        # That edit did not bring the total closer. One
                        # unhelpful fix is noise; two in a row means it
                        # is guessing, and guessing costs a full image
                        # round trip each time.
                        fruitless += 1
                        if fruitless >= 2:
                            payload = {
                                **payload,
                                "note": "your last two changes did not get "
                                        "closer. Stop adjusting and call "
                                        "submit; the remaining difference is "
                                        "recorded separately.",
                            }
                            stop = True
                    else:
                        fruitless = 0
            elif name == "submit":
                payload = pad.summary()
                if payload["matches"]:
                    submitted = True
            else:
                payload = {"error": f"unknown tool {name}"}

            messages.append({
                "role": "tool", "tool_name": name,
                "content": json.dumps(payload),
            })
            if submitted or stop:
                break

        if submitted:
            break

    closed = pad.difference_cents == 0
    remainder = pad.reconcile()
    return LoopResult(
        lines=pad.lines, target_cents=target_cents, tool_calls=calls,
        closed=closed, submitted=submitted, remainder=remainder,
        model=model_name, seconds=time.monotonic() - started,
        thinking_chars=thinking_chars,
    )
