"""Internal part number (IPN) formatting.

IPNs look like ``SK-00001``: the prefix comes from ``settings.ipn_prefix`` and
the number is zero-padded to five digits. Formatting is a pure function so it
can be unit-tested without touching the database.
"""

from __future__ import annotations

from .config import settings


def format_ipn(value: int, prefix: str | None = None) -> str:
    if value < 0:
        raise ValueError("IPN value must be non-negative")
    return f"{settings.ipn_prefix if prefix is None else prefix}-{value:05d}"
