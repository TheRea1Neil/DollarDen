"""A finished job must be storable, not just correct.

Caught in production, not here, and the symptom was maximally
misleading. `ai_jobs.provider` holds `type(provider).__name__` in a
`varchar(16)`. `DeckProvider` was twelve characters and fitted.
Renaming it to `ManagedOllamaProvider` made it twenty-one, so every
job ran, called its tools, got a correct answer from the model -- and
then threw on commit:

    StringDataRightTruncation: value too long for character varying(16)

The worker loop logged the crash and moved on, the row stayed
`running`, and fifteen minutes later the stale-job reaper marked it
"Timed out -- the server restarted or the AI host never answered."
Which was not true in any particular. From the app it looked exactly
like the AI had stopped working.

The gap was that no test ever wrote a real provider name. These check
every name the code can actually produce, so widening a class name
cannot quietly break saving a result again.
"""
from __future__ import annotations

import uuid

from sqlalchemy import inspect as sa_inspect

from app.ai import provider as provider_mod
from app.db.models import AIJob, User
from app.db.session import SessionLocal
from tests.conftest import requires_db


def _provider_class_names() -> list[str]:
    """Every provider class name this codebase can store."""
    out = []
    for name in dir(provider_mod):
        obj = getattr(provider_mod, name)
        if (isinstance(obj, type)
                and issubclass(obj, provider_mod.AIProvider)
                and obj is not provider_mod.AIProvider):
            out.append(obj.__name__)
    return sorted(out)


class TestEveryProviderNameFitsTheColumn:
    def test_the_names_are_what_we_think_they_are(self) -> None:
        """A guard on the guard: an empty list would pass everything."""
        names = _provider_class_names()
        assert len(names) >= 3, f"only found {names}"
        assert "ManagedOllamaProvider" in names

    def test_each_name_fits(self) -> None:
        width = AIJob.__table__.c.provider.type.length
        for name in _provider_class_names():
            assert len(name) <= width, (
                f"{name!r} is {len(name)} characters and the column holds "
                f"{width}. A job would run, answer, and fail on commit.")


@requires_db
class TestItActuallyStores:
    def test_the_longest_name_round_trips(self) -> None:
        """The column width is one thing; the database is the authority."""
        longest = max(_provider_class_names(), key=len)
        with SessionLocal() as db:
            u = User(email=f"jp-{uuid.uuid4().hex[:8]}@example.com",
                     password_hash="x")
            db.add(u); db.flush()
            job = AIJob(user_id=u.id, kind="ask", status="done",
                        request={}, provider=longest)
            db.add(job)
            db.commit()          # this is what used to raise
            db.refresh(job)
            assert job.provider == longest, "stored whole, not truncated"

    def test_the_model_and_the_database_agree_on_the_width(self) -> None:
        """A migration that was written but never applied looks like this."""
        with SessionLocal() as db:
            cols = sa_inspect(db.bind).get_columns("ai_jobs")
            live = next(c for c in cols if c["name"] == "provider")
            assert live["type"].length == AIJob.__table__.c.provider.type.length
