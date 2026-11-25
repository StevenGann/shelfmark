"""Shelfmark HTTP entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__, db
from .config import settings
from .parts_api import router as parts_api_router
from .web import router as web_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    yield


app = FastAPI(title="Shelfmark", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(parts_api_router)
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
