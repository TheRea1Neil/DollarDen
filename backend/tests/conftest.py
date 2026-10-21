from __future__ import annotations

import os

# Must be set before app.core.config is imported.
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "55433")
os.environ.setdefault("POSTGRES_USER", "nnf")
os.environ.setdefault("POSTGRES_PASSWORD", "devpw")
os.environ.setdefault("POSTGRES_DB", "nnf")
os.environ.setdefault("JWT_SIGNING_KEY", "test-signing-key-not-for-production")
os.environ.setdefault(
    "TOKEN_ENCRYPTION_KEY", "7Nn3dQ0kqYQ8xq3Yw1xS0dG8r2mE4vJ1aP6tK9uB5cY="
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="no Postgres on 127.0.0.1:55433")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Truncate between tests so ordering never matters."""
    if not _db_available():
        yield
        return
    yield
    with SessionLocal() as s:
        s.execute(
            text(
                "TRUNCATE allocations, review_items, receipts, ai_jobs, paychecks, "
                "split_rules, split_plans, category_rules, categories, transactions, "
                "accounts, plaid_items, user_goals, sessions, users RESTART IDENTITY CASCADE"
            )
        )
        s.commit()
