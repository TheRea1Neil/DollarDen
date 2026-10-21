"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import auth, backup, health, plaid
from app.core.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
log = logging.getLogger("nnf")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    # Loud about which Plaid environment is live -- the difference between
    # free sandbox calls and spending connection credits.
    log.info(
        "starting %s | plaid_env=%s allow_production=%s ai_provider=%s",
        s.app_name, s.plaid_env, s.allow_production, s.ai_provider,
    )
    if s.plaid_env == "production":
        log.warning("PLAID IS IN PRODUCTION MODE - linking consumes connection credits")
    yield


def create_app() -> FastAPI:
    s = get_settings()

    app = FastAPI(
        title=s.app_name,
        version="0.1.0",
        docs_url="/api/docs" if s.debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(plaid.router, prefix="/api")
    app.include_router(backup.router, prefix="/api")
    return app


app = create_app()
