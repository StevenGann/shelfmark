"""Shelfmark HTTP entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, db, enrich
from .boms_api import router as boms_api_router
from .builds_api import router as builds_api_router
from .config import settings
from .export_api import router as export_api_router
from .keys_api import router as keys_api_router
from .labels_api import router as labels_api_router
from .offers_api import router as offers_api_router
from .parts_api import router as parts_api_router
from .ratelimit import allow
from .stock_api import router as stock_api_router
from .subs_api import router as subs_api_router
from .web import router as web_router

STATIC_DIR = Path(__file__).resolve().parent / "static"

# uvicorn configures handlers for its own loggers only; a plain module logger
# would be swallowed by lastResort below WARNING and the tick would be silent.
logger = logging.getLogger("uvicorn.error")
_tick_lock = asyncio.Lock()


def _drain_queue() -> None:
    """One queue pass, run off the event loop."""
    conn = db.connect()
    try:
        processed = enrich.run_queue(conn)
        if processed:
            logger.info("queue tick processed %s job(s)", processed)
    finally:
        conn.close()


async def _queue_tick() -> None:
    while True:
        await asyncio.sleep(settings.queue_tick_seconds)
        if _tick_lock.locked():
            continue
        async with _tick_lock:
            try:
                await asyncio.to_thread(_drain_queue)
            except Exception:  # noqa: BLE001 - a bad tick must not kill the app
                logger.exception("queue tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auth == "none" and settings.env == "production":
        raise RuntimeError("SHELFMARK_AUTH=none is not allowed when SHELFMARK_ENV=production")
    db.migrate()
    conn = db.connect()
    try:
        auth.bootstrap(conn)
    finally:
        conn.close()

    tick: asyncio.Task[None] | None = None
    if settings.queue_tick_seconds > 0:
        logger.info("queue tick every %ss", settings.queue_tick_seconds)
        tick = asyncio.create_task(_queue_tick())
    try:
        yield
    finally:
        if tick is not None:
            tick.cancel()
            with suppress(asyncio.CancelledError):
                await tick


app = FastAPI(title="Shelfmark", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def rate_limit(request: Request, call_next) -> Response:
    limit = settings.rate_limit
    if limit > 0:
        raw = auth.extract_header_key(request)
        if raw:
            bucket, cap = "k:" + auth.hash_key(raw), limit
        else:
            token = request.cookies.get("smk_session")
            if token:
                bucket, cap = "s:" + auth.hash_key(token), limit
            else:
                ip = request.client.host if request.client else "unknown"
                bucket, cap = f"ip:{ip}", max(1, limit // 10)
        wait = allow(bucket, cap)
        if wait:
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(wait)},
            )
    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(parts_api_router)
app.include_router(stock_api_router)
app.include_router(boms_api_router)
app.include_router(builds_api_router)
app.include_router(keys_api_router)
app.include_router(export_api_router)
app.include_router(labels_api_router)
app.include_router(offers_api_router)
app.include_router(subs_api_router)
app.include_router(web_router)


@app.get("/api/health")
def health() -> dict:
    ok = db.check()
    return {
        "status": "ok" if ok else "error",
        "db": "ok" if ok else "fail",
        "version": __version__,
        "data_dir": str(settings.data_dir),
    }
