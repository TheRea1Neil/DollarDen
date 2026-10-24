"""Finding the charges that repeat.

This is the insight people are worst at producing unaided. Nobody can
list their subscriptions from memory, and the ones they forget are
exactly the ones still billing. It is also entirely computable from
dates and amounts, so it needs no model and cannot hallucinate a
service you do not pay for.

Detection is deliberately conservative. A false positive here tells
someone they have a subscription they do not have, which sends them
hunting through statements for nothing; a false negative just means one
line is missing from a list they can still read. So: at least three
charges, a regular interval, and a stable amount.
"""
from __future__ import annotations

import re
import statistics
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Allocation, Transaction

# (name, low, high, per-year) in days. Ranges are wide enough to absorb
# weekend shifts and "the 1st of the month" landing 28 to 31 days apart.
CADENCES: tuple[tuple[str, int, int, float], ...] = (
    ("weekly", 6, 8, 52.0),
    ("fortnightly", 13, 16, 26.0),
    ("monthly", 27, 34, 12.0),
    ("two-monthly", 55, 66, 6.0),
    ("quarterly", 85, 96, 4.0),
    ("half-yearly", 175, 190, 2.0),
    ("yearly", 355, 378, 1.0),
)

MIN_CHARGES = 3
# Price rises are normal; a merchant whose amount swings more than this
# is a shop you visit often, not a subscription.
AMOUNT_TOLERANCE = 0.15
# Allowed spread in the gaps, as a fraction of the median gap.
INTERVAL_TOLERANCE = 0.25

_STORE_SUFFIX = re.compile(r"[\s#*-]+\d{2,}\s*$")
_CARD_PREFIX = re.compile(r"^(sq|tst|pp|paypal|sp|pos|ach)\s*\*\s*", re.I)


def normalise_merchant(raw: str) -> str:
    """Collapse the variants one merchant arrives under.

    "SQ *BLUE BOTTLE 4821" and "SQ *BLUE BOTTLE 9930" are the same
    coffee shop with two till numbers; left apart they each fall under
    the three-charge floor and the subscription disappears.
    """
    s = (raw or "").strip()
    s = _CARD_PREFIX.sub("", s)
    s = _STORE_SUFFIX.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" *-#").lower()


@dataclass
class Recurring:
    merchant: str
    cadence: str
    typical_cents: int
    charge_count: int
    first_seen: date
    last_seen: date
    next_expected: date | None
    annual_cents: int
    confidence: float
    # True when the next charge is well overdue -- probably cancelled,
    # and worth saying so rather than counting it in a yearly total.
    likely_ended: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "merchant": self.merchant,
            "cadence": self.cadence,
            "typical_cents": self.typical_cents,
            "charge_count": self.charge_count,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "next_expected": self.next_expected.isoformat() if self.next_expected else None,
            "annual_cents": self.annual_cents,
            "confidence": round(self.confidence, 2),
            "likely_ended": self.likely_ended,
        }


def _classify(gaps: list[int]) -> tuple[str, float, float] | None:
    """(cadence, per-year, regularity) for a run of gaps, or None."""
    if not gaps:
        return None
    med = statistics.median(gaps)
    for name, lo, hi, per_year in CADENCES:
        if lo <= med <= hi:
            spread = max(abs(g - med) for g in gaps)
            if med and spread / med > INTERVAL_TOLERANCE:
                return None
            regularity = 1.0 - (spread / med if med else 1.0)
            return name, per_year, max(0.0, regularity)
    return None


def detect(
    db: Session, user_id: uuid.UUID, *, today: date, lookback_days: int = 400
) -> list[Recurring]:
    since = today - timedelta(days=lookback_days)
    rows = db.execute(
        select(
            func.coalesce(Transaction.merchant_name, Transaction.name).label("merchant"),
            Transaction.date,
            func.sum(func.abs(Allocation.amount_cents)).label("cents"),
        )
        .join(Allocation, Allocation.transaction_id == Transaction.id)
        .where(
            Allocation.user_id == user_id,
            Allocation.amount_cents < 0,
            Transaction.removed_at.is_(None),
            Transaction.date >= since,
            Transaction.date <= today,
        )
        # One row per shopping trip, even when the receipt was itemised
        # across several envelopes.
        .group_by(Transaction.id, Transaction.merchant_name, Transaction.name, Transaction.date)
    ).all()

    by_merchant: dict[str, list[tuple[date, int, str]]] = {}
    for r in rows:
        key = normalise_merchant(r.merchant)
        if not key:
            continue
        by_merchant.setdefault(key, []).append((r.date, int(r.cents), r.merchant))

    found: list[Recurring] = []
    for key, charges in by_merchant.items():
        if len(charges) < MIN_CHARGES:
            continue
        charges.sort(key=lambda c: c[0])
        dates = [c[0] for c in charges]
        amounts = [c[1] for c in charges]

        med_amount = int(statistics.median(amounts))
        if med_amount <= 0:
            continue
        if max(abs(a - med_amount) for a in amounts) / med_amount > AMOUNT_TOLERANCE:
            continue

        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        verdict = _classify(gaps)
        if verdict is None:
            continue
        cadence, per_year, regularity = verdict

        med_gap = int(statistics.median(gaps))
        next_expected = dates[-1] + timedelta(days=med_gap)
        overdue_by = (today - next_expected).days
        likely_ended = overdue_by > max(7, med_gap // 2)

        # More observations is more evidence, capped so a long-running
        # charge does not claim certainty it has not earned.
        evidence = min(1.0, len(charges) / 6.0)
        confidence = round(0.5 * regularity + 0.5 * evidence, 3)

        found.append(
            Recurring(
                # Show the merchant as the bank writes it, not the
                # lowercased key used for matching.
                merchant=charges[-1][2],
                cadence=cadence,
                typical_cents=med_amount,
                charge_count=len(charges),
                first_seen=dates[0],
                last_seen=dates[-1],
                next_expected=None if likely_ended else next_expected,
                annual_cents=int(round(med_amount * per_year)),
                confidence=confidence,
                likely_ended=likely_ended,
            )
        )

    found.sort(key=lambda r: (r.likely_ended, -r.annual_cents))
    return found


def monthly_commitment_cents(items: list[Recurring]) -> int:
    """What the live subscriptions cost in an average month."""
    return int(round(sum(r.annual_cents for r in items if not r.likely_ended) / 12))
