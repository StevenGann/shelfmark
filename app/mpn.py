"""Manufacturer part number (MPN) normalisation.

Two MPNs that differ only in punctuation are the same part; two that differ in
a digit are not. Normalising to upper-case letters and digits only makes that
comparison exact.
"""

from __future__ import annotations


def normalise_mpn(s: str) -> str:
    """Upper-case *s* and strip everything that is not a letter or digit.

    Raises ``ValueError`` when nothing remains, so an empty or punctuation-only
    MPN is rejected rather than silently becoming the empty string.
    """
    result = "".join(ch for ch in s.upper() if ch.isalnum())
    if not result:
        raise ValueError("MPN must contain at least one letter or digit")
    return result
