"""Creating and tidying expense types.

The whole value of this axis is that the same kind of spending always
lands on the same label. A taxonomy containing Fast food, Fast Food and
Quick Service Restaurant answers "how much on fast food" with a third
of the truth, and does it confidently, which is worse than refusing.

So every route into creation passes through here, and here does three
things: normalises the name, refuses a near-duplicate of something that
already exists, and records who coined it so the user can review what
the assistant invented.

The starter vocabulary below is offered to the model but NOT created up
front. A fresh account with forty empty types is clutter, and an empty
account gives the model nothing to anchor on and invites it to make up
a new phrase for every charge. Offering names that only become real
when something is filed under them gets both.
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import Allocation, ExpenseType

MAX_NAME = 40


class TaxonomyError(ValueError):
    """A type that cannot be created as asked."""


# A conventional starting vocabulary. Deliberately shaped around how
# people ask questions -- "fast food" and "groceries" are separate
# because nobody asks about "food" -- and kept short enough that the
# model can hold all of it while choosing.
STARTER_TYPES: tuple[tuple[str, str], ...] = (
    ("Groceries", "Supermarket and grocery shopping for food cooked at home."),
    ("Fast food", "Counter-service and drive-through food: burgers, pizza, chicken, tacos."),
    ("Restaurants", "Sit-down meals and table service."),
    ("Coffee", "Coffee shops and cafes."),
    ("Fuel", "Petrol and charging for a vehicle."),
    ("Transport", "Rideshare, taxis, transit fares, parking and tolls."),
    ("Subscriptions", "Charges that repeat on a schedule: streaming, software, memberships."),
    ("Utilities", "Power, water, gas, internet and phone bills."),
    ("Rent or mortgage", "Housing payment."),
    ("Insurance", "Any insurance premium."),
    ("Medical", "Doctors, dentists, pharmacy and medical supplies."),
    ("Household", "Cleaning supplies, kitchen goods and small things for the home."),
    ("Home maintenance", "Repairs, tools, garden and improvements to the home."),
    ("Clothing", "Clothes, shoes and accessories."),
    ("Personal care", "Haircuts, toiletries and grooming."),
    ("Entertainment", "Films, games, events, books and hobbies."),
    ("Travel", "Flights, hotels and holidays."),
    ("Gifts", "Presents for other people."),
    ("Charity and giving", "Donations, tithing and offerings."),
    ("Education", "Tuition, courses, school fees and learning materials."),
    ("Childcare", "Nursery, babysitting and children's activities."),
    ("Pets", "Food, vets and supplies for animals."),
    ("Fees and interest", "Bank fees, card interest, late charges."),
    ("Taxes", "Tax payments."),
    ("Debt payment", "Paying down a loan or card balance."),
    ("Savings transfer", "Money moved into savings or investment."),
)

# Words that carry no distinguishing meaning in a type name. Stripped
# before the duplicate check so "Fast Food Restaurants" and "Fast food"
# collide instead of coexisting.
_FILLER = {"and", "or", "the", "a", "an", "of", "other", "misc", "miscellaneous", "general"}


def slugify(name: str) -> str:
    """Normalise for comparison, not for display.

    Lowercases, drops punctuation, strips filler words and naive
    plurals. "Fast Food", "fast-food" and "Fast Foods" all reduce to
    "fast food".
    """
    s = (name or "").lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    words = [w for w in s.split() if w and w not in _FILLER]
    out = []
    for w in words:
        # Crude but adequate: these names are short English noun
        # phrases, not arbitrary text.
        if len(w) > 3 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("ses"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return " ".join(out)[:MAX_NAME] or "unnamed"


def active(db: Session, user_id: uuid.UUID) -> list[ExpenseType]:
    return list(
        db.scalars(
            select(ExpenseType)
            .where(ExpenseType.user_id == user_id, ExpenseType.archived_at.is_(None))
            .order_by(func.lower(ExpenseType.name))
        ).all()
    )


def find_by_slug(db: Session, user_id: uuid.UUID, slug: str) -> ExpenseType | None:
    return db.scalars(
        select(ExpenseType).where(
            ExpenseType.user_id == user_id,
            ExpenseType.slug == slug,
            ExpenseType.archived_at.is_(None),
        )
    ).first()


def resolve_or_create(
    db: Session,
    user_id: uuid.UUID,
    name: str,
    *,
    description: str | None = None,
    created_by: str = "user",
) -> tuple[ExpenseType, bool]:
    """Get the type for this name, creating it only if nothing matches.

    Returns (type, created). Matching is on the slug, so asking for
    "Fast Foods" when "Fast food" exists returns the existing one
    rather than forking the taxonomy.
    """
    name = (name or "").strip()
    if not name:
        raise TaxonomyError("an expense type needs a name")
    if len(name) > MAX_NAME:
        raise TaxonomyError(f"expense type names are limited to {MAX_NAME} characters")

    slug = slugify(name)
    existing = find_by_slug(db, user_id, slug)
    if existing:
        # Backfill a description if the existing row has none and the
        # caller brought one.
        if description and not existing.description:
            existing.description = description.strip()
        return existing, False

    seeded = {slugify(n): (n, d) for n, d in STARTER_TYPES}
    if slug in seeded and not description:
        name, description = seeded[slug]

    et = ExpenseType(
        user_id=user_id,
        name=name,
        slug=slug,
        description=(description or "").strip() or None,
        created_by=created_by,
        # Anything the user typed is reviewed by definition. A type the
        # model coined waits for a look.
        reviewed_at=datetime.now(UTC) if created_by == "user" else None,
    )
    db.add(et)
    db.flush()
    return et, True


def unreviewed(db: Session, user_id: uuid.UUID) -> list[ExpenseType]:
    """Types the assistant invented that nobody has blessed yet."""
    return list(
        db.scalars(
            select(ExpenseType).where(
                ExpenseType.user_id == user_id,
                ExpenseType.archived_at.is_(None),
                ExpenseType.reviewed_at.is_(None),
            )
            .order_by(ExpenseType.created_at.desc())
        ).all()
    )


def merge(db: Session, user_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID) -> int:
    """Fold one type into another, repointing its history.

    The allocations are relabelled rather than left pointing at a
    tombstone, so every query stays a plain join. This edits history,
    which the ledger forbids for MONEY -- but an expense type is a
    label on money, not money, and a merge that left old spending
    under the old name would defeat the point of merging.

    Returns how many allocations moved.
    """
    if source_id == target_id:
        raise TaxonomyError("cannot merge a type into itself")

    rows = db.scalars(
        select(ExpenseType).where(
            ExpenseType.user_id == user_id, ExpenseType.id.in_((source_id, target_id))
        )
    ).all()
    found = {r.id: r for r in rows}
    if source_id not in found or target_id not in found:
        raise TaxonomyError("both types must exist and belong to this user")
    if found[target_id].archived_at is not None:
        raise TaxonomyError("cannot merge into an archived type")

    moved = db.execute(
        update(Allocation)
        .where(Allocation.user_id == user_id, Allocation.expense_type_id == source_id)
        .values(expense_type_id=target_id)
    ).rowcount

    src = found[source_id]
    src.archived_at = datetime.now(UTC)
    src.merged_into_id = target_id
    db.flush()
    return int(moved or 0)


def suggestions_for_prompt(db: Session, user_id: uuid.UUID) -> list[dict[str, str]]:
    """What to show the model: everything in use, plus unused standards.

    Existing types come first and are marked, because reusing one is
    always preferable to coining a near-synonym.
    """
    mine = active(db, user_id)
    out = [
        {"name": t.name, "description": t.description or "", "status": "existing"}
        for t in mine
    ]
    taken = {t.slug for t in mine}
    out += [
        {"name": n, "description": d, "status": "available"}
        for n, d in STARTER_TYPES
        if slugify(n) not in taken
    ]
    return out
