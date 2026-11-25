"""Shelfmark HTTP entry point. M0: health only."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__, db
from .config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    yield


app = FastAPI(title="Shelfmark", version=__version__, lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    ok = db.check()
    return {
        "status": "ok" if ok else "error",
        "db": "ok" if ok else "fail",
        "version": __version__,
        "data_dir": str(settings.data_dir),
    }
