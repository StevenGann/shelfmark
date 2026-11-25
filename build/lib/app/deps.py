"""Shared FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends

from . import db

Conn = Annotated[sqlite3.Connection, Depends(db.connection)]
