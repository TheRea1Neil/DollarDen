"""Remembering where a receipt line was filed.

The problem is specific. Supermarket slips print truncated codes --
COTTAGE CH, GV MXDVG 12Z, HF SMK SGE -- and a model cannot reliably
reason about them. On the first real receipt it filed the legible lines
and declined the rest, which is right for a guess and useless as a
recurring experience: the same questions come back every shop.

These names are unlearnable by reasoning and trivial to learn by
memory, so the app learns them instead of asking the model harder.

WHAT THIS IS NOT
----------------
It is not a CategoryRule. Those match whole transactions and run inside
auto-categorise, where a bad one silently redirects real money -- a
`fun` matcher on this account was catching FUNDRAISER, and a
"remember this vendor" feature once produced 22 rules with 11 exact
duplicates. A memory here only ever PRE-FILLS a line on a screen the
user is looking at before they submit. Wrong is one tap to change, not
money in the wrong envelope.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.recurring import normalise_merchant
from app.db.models import ItemMemory

MAX_SLUG = 120

# Product codes, prices and the single-letter tax flags printed beside
# them. Stripped so "COTTAGE CH 015700251020 F 2.88 R" and a later
# "COTTAGE CH" with a different barcode are the same memory.
_CODE = re.compile(r"\b\d{5,}\b")
_PRICE = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")
_FLAG = re.compile(r"\b[A-Z]\b")
_PACK = re.compile(r"\b\d+\s*(oz|lb|lbs|ct|pk|g|kg|ml|l|z)\b", re.I)


def slug(description: str) -> str:
    """Normalise a printed line to something that matches next time.

    Pack sizes go too: "GV BFLOR 12" and "GV BFLOR 16" are the same
    product in a different tin, and filing them apart would make the
    memory need teaching twice for no gain.
    """
    s = (description or "").strip()
    s = _PRICE.sub(" ", s)
    s = _CODE.sub(" ", s)
    s = _FLAG.sub(" ", s)
    s = _PACK.sub(" ", s)
    s = re.sub(r"[^A-Za-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    # A trailing bare number is almost always a size.
    s = re.sub(r"\s+\d+$", "", s)
    return s[:MAX_SLUG]


@dataclass(frozen=True)
class Recollection:
    """What is known about one line, and how confidently."""

    category_id: uuid.UUID
    expense_type_id: uuid.UUID | None
    description: str
    times_seen: int
    # "merchant" -- learned at this shop, the strong case.
    # "shared"   -- learned elsewhere, and every shop that knows it
    #               agrees, so it is safe to carry across.
    source: str

    def as_dict(self) -> dict:
        return {
            "category_id": str(self.category_id),
            "expense_type_id": (
                str(self.expense_type_id) if self.expense_type_id else None
            ),
            "description": self.description,
            "times_seen": self.times_seen,
            "source": self.source,
        }


def _same_shop(stored: str, here: str) -> bool:
    """Whether two normalised merchant names are the same shop.

    Exact match, or one is a whole-word prefix of the other. Chains
    print their format in the name -- "WALMART SUPERCENTER #1234" and
    "Walmart #9999" normalise to "walmart supercenter" and "walmart" --
    and item codes are chain-wide, so they should share a memory.

    Prefix rather than a list of chain names and their store formats:
    hard-coding "supercenter" and "neighborhood market" is exactly the
    over-fitting that made the receipt line filter eat real purchases.
    The word boundary stops "target" matching "targeted goods".
    """
    if not stored or not here:
        return False
    if stored == here:
        return True
    short, long = sorted((stored, here), key=len)
    return long.startswith(short + " ")


def recall(
    db: Session, user_id: uuid.UUID, merchant: str, descriptions: Iterable[str]
) -> dict[str, Recollection]:
    """Look up each description, keyed by the ORIGINAL string given.

    Returns only what is known. A line with no memory is simply absent,
    which leaves it for the model or the human.
    """
    wanted = {d: slug(d) for d in descriptions if slug(d)}
    if not wanted:
        return {}

    slugs = set(wanted.values())
    rows = db.scalars(
        select(ItemMemory).where(
            ItemMemory.user_id == user_id,
            ItemMemory.description_slug.in_(slugs),
        )
    ).all()
    if not rows:
        return {}

    here = normalise_merchant(merchant)
    by_slug: dict[str, list[ItemMemory]] = {}
    for r in rows:
        by_slug.setdefault(r.description_slug, []).append(r)

    out: dict[str, Recollection] = {}
    for original, s in wanted.items():
        candidates = by_slug.get(s) or []
        if not candidates:
            continue

        # This shop's own memory always wins.
        mine = next((c for c in candidates if _same_shop(c.merchant_slug, here)), None)
        if mine is not None:
            out[original] = Recollection(
                mine.category_id, mine.expense_type_id, mine.description,
                mine.times_seen, "merchant",
            )
            continue

        # Otherwise borrow from other shops, but only when they are
        # unanimous. Two chains filing the same abbreviation differently
        # is exactly the case where guessing is worse than asking.
        categories = {c.category_id for c in candidates}
        if len(categories) != 1:
            continue
        best = max(candidates, key=lambda c: c.times_seen)
        types = {c.expense_type_id for c in candidates}
        out[original] = Recollection(
            best.category_id,
            best.expense_type_id if len(types) == 1 else None,
            best.description,
            sum(c.times_seen for c in candidates),
            "shared",
        )
    return out


def remember(
    db: Session,
    user_id: uuid.UUID,
    merchant: str,
    description: str,
    category_id: uuid.UUID,
    expense_type_id: uuid.UUID | None = None,
) -> ItemMemory | None:
    """Record, or reinforce, where this line belongs at this shop.

    Returns None for a description that normalises to nothing -- a line
    that is only a price or a barcode teaches nothing.
    """
    s = slug(description)
    if not s:
        return None
    merchant_slug = normalise_merchant(merchant)[:MAX_SLUG]
    if not merchant_slug:
        return None

    row = db.scalars(
        select(ItemMemory).where(
            ItemMemory.user_id == user_id,
            ItemMemory.merchant_slug == merchant_slug,
            ItemMemory.description_slug == s,
        )
    ).first()

    now = datetime.now(UTC)
    if row is None:
        row = ItemMemory(
            user_id=user_id,
            merchant_slug=merchant_slug,
            description_slug=s,
            description=description.strip()[:200],
            category_id=category_id,
            expense_type_id=expense_type_id,
            times_seen=1,
            last_seen_at=now,
        )
        db.add(row)
    else:
        # A correction overwrites rather than accumulating. The last
        # thing the user chose is what they meant; a memory that needed
        # changing should not need changing twice.
        changed = row.category_id != category_id
        row.category_id = category_id
        if expense_type_id is not None:
            row.expense_type_id = expense_type_id
        row.description = description.strip()[:200]
        row.times_seen = 1 if changed else row.times_seen + 1
        row.last_seen_at = now
    db.flush()
    return row


def forget(db: Session, user_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
    row = db.scalars(
        select(ItemMemory).where(
            ItemMemory.id == memory_id, ItemMemory.user_id == user_id
        )
    ).first()
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True
