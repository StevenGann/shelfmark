"""Component package normalisation.

DigiKey writes packages like ``0402 (1005 Metric)``, ``40-VFQFN Exposed Pad``
and ``8-SOIC (0.154", 3.90mm Width)``. The human-facing string is kept in full,
but a searchable key is derived so that the same package written two ways sorts
and filters together. The key is deliberately conservative: when nothing
matches, the original string is returned rather than inventing a package.
"""

from __future__ import annotations

import re

IMPERIAL_CHIP_SIZES = ("0201", "0402", "0603", "0805", "1206", "1210", "1812", "2512")

# DigiKey writes ``40-VFQFN``; VQFN and VFQFN are the same family for search,
# so the ``F`` (fine pitch) is folded into the canonical ``VQFN`` key.
_FAMILY_SYNONYMS = {"VFQFN": "VQFN"}

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NUMBER_FAMILY = re.compile(r"^(\d+)-([A-Za-z]+)$")


def normalise_package(package: str) -> str:
    """Return a searchable key for *package*, or the input unchanged.

    Rules, in order:

    - imperial chip sizes 0201, 0402, 0603, 0805, 1206, 1210, 1812 and 2512 are
      taken from the leading token;
    - ``<n>-<family>`` becomes ``<FAMILY>-<n>``, upper-cased (``8-SOIC`` ->
      ``SOIC-8``, ``40-VFQFN`` -> ``VQFN-40``);
    - parentheticals such as ``(1005 Metric)`` are dropped;
    - anything else is returned unchanged.
    """
    if not package:
        return package
    stripped = _PARENTHETICAL.sub(" ", package)
    tokens = stripped.split()
    if not tokens:
        return package
    lead = tokens[0].strip('",;')
    if lead in IMPERIAL_CHIP_SIZES:
        return lead
    match = _NUMBER_FAMILY.fullmatch(lead)
    if match:
        family = match.group(2).upper()
        family = _FAMILY_SYNONYMS.get(family, family)
        return f"{family}-{match.group(1)}"
    return package
