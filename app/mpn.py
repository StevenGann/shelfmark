"""Manufacturer part number (MPN) normalisation.

Two MPNs that differ only in punctuation are the same part; two that differ in
a digit are not. Normalising to upper-case letters and digits only makes that
comparison exact.
"""

from __future__ import annotations

import unicodedata


def normalise_mpn(s: str) -> str:
    """Upper-case *s* and strip everything that is not a letter or digit.

    Raises ``ValueError`` when nothing remains, so an empty or punctuation-only
    MPN is rejected rather than silently becoming the empty string.
    """
    result = "".join(ch for ch in s.upper() if ch.isalnum())
    if not result:
        raise ValueError("MPN must contain at least one letter or digit")
    return result


def fold_name(value: str) -> str:
    """Compare two manufacturer names without arguing about accents or case.

    "Würth Elektronik" and "Wurth Elektronik" are one company, and a BOM that
    spells it differently from the distributor is not a different part. Only
    used for matching; the name as written stays in the database.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def normalise_value(value: str) -> str:
    """Fold a passive value into one search key.

    ``2.2uF``, ``2.2 µF`` and ``2.2UF`` all become ``2.2uf`` so that two parts
    are recognisably the same component without the catalogue enforcing a
    spelling.
    """
    folded = value.casefold().replace("\u00b5", "u").replace("\u03bc", "u")
    return "".join(ch for ch in folded if not ch.isspace())
