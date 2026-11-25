"""Shelfmark HTTP entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, db
from .config import settings
from .keys_api import router as keys_api_router
from .parts_api import router as parts_api_router
from .ratelimit import allow
from .web import router as web_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


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
    yield


app = FastAPI(title="Shelfmark", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def rate_limit(request: Request, call_next) -> Response:
    limit = settings.rate_limit
    if limit > 0:
        raw = auth.extract_header_key(request)
        if raw:
            bucket, cap = "k:" + auth.hash_key(raw), limit
        else:
            key_id = auth.key_id_from_cookie(request)
            if key_id is not None:
                bucket, cap = f"k:{key_id}", limit
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
app.include_router(keys_api_router)
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
