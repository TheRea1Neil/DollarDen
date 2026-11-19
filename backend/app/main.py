"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
import contextlib
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import (
    admin,
    ai,
    analytics,
    auth,
    backup,
    budget,
    health,
    devdocs as devdocs_routes,
    item_memory,
    mcp,
    plaid,
)
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

    # Drain the AI job queue in the background. Jobs are persisted, so a
    # restart loses nothing: in-flight rows are reconciled by the worker.
    import asyncio

    from app.ai.jobs import worker_loop
    from app.plaid.scheduled import interval_minutes, sync_loop

    tasks = [asyncio.create_task(worker_loop())]

    # Pull from Plaid on a schedule as well as on webhooks. Required on
    # a plain-HTTP install, which cannot receive webhooks at all, and a
    # backstop everywhere else -- a missed webhook otherwise means
    # transactions stop arriving with nothing reporting an error.
    if s.plaid_sync_enabled:
        log.info("Plaid sync schedule: every %d minutes", interval_minutes(s))
        tasks.append(asyncio.create_task(sync_loop()))
    else:
        log.info("Plaid sync schedule disabled; webhooks only")

    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


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
    app.include_router(budget.router, prefix="/api")
    app.include_router(ai.router, prefix="/api")
    app.include_router(analytics.router, prefix="/api")
    app.include_router(item_memory.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")
    app.include_router(mcp.router, prefix="/api")
    app.include_router(devdocs_routes.router, prefix="/api")

    # The settings portal: plain HTML, CSS and JavaScript, served by
    # the same process as the API it talks to.
    #
    # Here rather than in the phone app because of a chicken and egg:
    # this is where the server's Plaid credentials and its very first
    # account are set, and the app cannot configure a server it has not
    # been told how to reach. A browser on the machine someone just
    # installed this on needs no such introduction.
    #
    # `html=True` serves index.html for /admin, so the one page works
    # at the address a person would actually type.
    from fastapi.staticfiles import StaticFiles

    web = Path(__file__).parent / "web"
    if web.is_dir():
        app.mount("/admin", StaticFiles(directory=str(web), html=True),
                  name="admin")
    else:  # pragma: no cover - only if the image was built wrong
        log.warning("admin portal files are missing; /admin will 404")
    return app


app = create_app()
