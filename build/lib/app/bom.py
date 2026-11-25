"""KiCad BOM CSV parsing.

KiCad writes a CSV whose headers depend on the version and the columns the
operator ticked, so the parser maps header *aliases* rather than positions and
sniffs the delimiter. The output is a list of :class:`ParsedLine`, collapsed by
``(mpn_norm, manufacturer_norm)`` and ready for the matching step in
``repo.boms``.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import TypedDict

from .mpn import normalise_mpn
from .packages import normalise_package

# Accepted header aliases per field. Headers are matched case-insensitively
# after stripping spaces and punctuation, so "Mfr Part #" and "manufacturer part
# number" both land on ``mpn``.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "references": (
        "Reference",
        "References",
        "Refs",
        "Ref",
        "Designator",
        "Designators",
        # KiCad writes this when the symbol's reference field was renamed, and
        # it is what the real sk-sensor export carries.
        "Customer Reference",
        "Customer References",
        "Part Reference",
    ),
    "quantity": ("Qty", "Quantity", "Count", "Quantity Per Board"),
    "value": ("Value", "Comment"),
    "footprint": ("Footprint", "Package", "Package / Footprint", "Case"),
    "mpn": (
        "MPN",
        "BOM MPN",
        "Manufacturer Part Number",
        "Manufacturer PN",
        "Mfr Part #",
        "Mfr Part Number",
        "Part Number",
        "Part #",
        "Supplier Part Number",
    ),
    "manufacturer": ("Manufacturer", "Mfr", "Mfr."),
    "description": ("Description", "Desc"),
    "dnp": ("DNP", "Do Not Populate", "Exclude from BOM"),
}

_DELIMITERS = (",", ";", "\t")
_REFERENCE_SPLIT = re.compile(r"[,;\s]+")
_RANGE_RE = re.compile(r"^([A-Za-z]+)(\d+)-([A-Za-z]+)(\d+)$")
_DIGITS_RE = re.compile(r"(\d+)")

_FALSE_DNP = {"", "0", "false", "no", "n", "none"}


class BomError(Exception):
    """A BOM file cannot be parsed or imported."""


class NoMpnColumnError(BomError):
    """The file has no column that maps to an MPN.

    ``headers`` carries the headers the parser actually saw, which is the
    information an operator needs to fix the export.
    """

    def __init__(self, headers: list[str]) -> None:
        self.headers = headers
        super().__init__(f"no MPN column found; saw headers: {', '.join(headers)}")


@dataclass
class ParsedLine:
    """One collapsed BOM line, before matching against parts."""

    mpn: str
    mpn_norm: str
    manufacturer: str | None
    manufacturer_norm: str | None
    description: str | None
    value: str | None
    footprint: str | None
    package: str | None
    quantity: int
    references: list[str]
    dnp: bool
    note: str | None


def _norm_header(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum()).lower()


_ALIAS_BY_NORM: dict[str, str] = {
    _norm_header(alias): field for field, aliases in HEADER_ALIASES.items() for alias in aliases
}


def natural_key(value: str) -> list[object]:
    """Sort key that orders ``C2`` before ``C10``.

    ``re.split`` on digits always yields alternating non-digit/digit chunks, so
    at any one index both keys hold a string or both hold an integer and the
    comparison never mixes the two.
    """
    return [int(p) if p.isdigit() else p.lower() for p in _DIGITS_RE.split(value)]


def _sniff_delimiter(text: str) -> str:
    sample = text[:4096]
    counts = {d: sample.count(d) for d in _DELIMITERS}
    best = max(_DELIMITERS, key=lambda d: counts[d])
    return best if counts[best] else ","


def _expand_token(token: str) -> list[str]:
    match = _RANGE_RE.fullmatch(token)
    if match is None or match.group(1) != match.group(3):
        return [token]
    prefix = match.group(1)
    start = int(match.group(2))
    end = int(match.group(4))
    if end <= start:
        return [token]
    width = len(match.group(2))
    return [f"{prefix}{i:0{width}d}" for i in range(start, end + 1)]


def expand_references(raw: str) -> list[str]:
    """Split a reference list and expand ranges, sorted naturally.

    ``"R1 R3, R2; C1-C3"`` becomes ``["C1", "C2", "C3", "R1", "R2", "R3"]``.
    """
    refs: list[str] = []
    seen: set[str] = set()
    for token in _REFERENCE_SPLIT.split(raw.strip()):
        if not token:
            continue
        for ref in _expand_token(token):
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    refs.sort(key=lambda ref: (natural_key(ref), ref))
    return refs


def _is_dnp(value: str) -> bool:
    return value.strip().lower() not in _FALSE_DNP


def _map_columns(header: list[str]) -> dict[int, str]:
    columns: dict[int, str] = {}
    for idx, name in enumerate(header):
        field = _ALIAS_BY_NORM.get(_norm_header(name))
        if field is not None:
            columns[idx] = field
    return columns


def _field(row: list[str], idx: int) -> str:
    return row[idx].strip() if idx < len(row) else ""


class _RawRow(TypedDict):
    mpn: str
    mpn_norm: str
    manufacturer: str | None
    manufacturer_norm: str | None
    description: str | None
    value: str | None
    footprint: str | None
    package: str | None
    quantity: int
    references: list[str]
    dnp: bool
    note: str | None


def _parse_row(row: list[str], columns: dict[int, str], row_number: int) -> _RawRow | None:
    cells = {field: _field(row, idx) for idx, field in columns.items()}
    if not any(cells.values()):
        return None
    mpn = cells.get("mpn", "")
    if not mpn:
        return None
    try:
        mpn_norm = normalise_mpn(mpn)
    except ValueError as exc:
        raise BomError(f"row {row_number}: {exc}") from exc

    manufacturer = cells.get("manufacturer") or None
    value = cells.get("value") or None
    footprint = cells.get("footprint") or None
    description = cells.get("description") or None

    references = expand_references(cells.get("references", ""))
    stated_qty = cells.get("quantity", "")
    notes: list[str] = []
    if references:
        quantity = len(references)
        if stated_qty:
            try:
                stated = int(stated_qty)
            except ValueError:
                notes.append(f"Qty {stated_qty!r} is not a number")
            else:
                if stated != quantity:
                    notes.append(f"Qty {stated} disagrees with {quantity} reference(s)")
    else:
        quantity = 1
        if stated_qty:
            try:
                quantity = int(stated_qty)
            except ValueError:
                notes.append(f"Qty {stated_qty!r} is not a number")

    return {
        "mpn": mpn,
        "mpn_norm": mpn_norm,
        "manufacturer": manufacturer,
        "manufacturer_norm": manufacturer.strip().lower() if manufacturer else None,
        "description": description,
        "value": value,
        "footprint": footprint,
        "package": normalise_package(footprint) if footprint else None,
        "quantity": quantity,
        "references": references,
        "dnp": _is_dnp(cells.get("dnp", "")),
        "note": "; ".join(notes) or None,
    }


def _collapse(rows: list[_RawRow]) -> list[ParsedLine]:
    grouped: dict[tuple[str, str], list[_RawRow]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (row["mpn_norm"], row["manufacturer_norm"] or "")
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    out: list[ParsedLine] = []
    for key in order:
        group = grouped[key]
        references: list[str] = []
        seen: set[str] = set()
        for row in group:
            for ref in row["references"]:
                if ref not in seen:
                    seen.add(ref)
                    references.append(ref)
        references.sort(key=lambda ref: (natural_key(ref), ref))
        quantity = len(references) if references else sum(r["quantity"] for r in group)

        notes: list[str] = []
        for row in group:
            if row["note"]:
                notes.append(row["note"])
        values = {r["value"] for r in group if r["value"]}
        if len(values) > 1:
            notes.append("value differs: " + "; ".join(sorted(str(v) for v in values)))
        footprints = {r["footprint"] for r in group if r["footprint"]}
        if len(footprints) > 1:
            notes.append("footprint differs: " + "; ".join(sorted(str(f) for f in footprints)))

        first = group[0]
        value = next((r["value"] for r in group if r["value"]), None)
        footprint = next((r["footprint"] for r in group if r["footprint"]), None)
        out.append(
            ParsedLine(
                mpn=first["mpn"],
                mpn_norm=first["mpn_norm"],
                manufacturer=first["manufacturer"],
                manufacturer_norm=first["manufacturer_norm"],
                description=first["description"],
                value=value,
                footprint=footprint,
                package=normalise_package(footprint) if footprint else None,
                quantity=quantity,
                references=references,
                dnp=any(r["dnp"] for r in group),
                note="; ".join(notes) or None,
            )
        )
    return out


def parse_bom(data: str | bytes) -> list[ParsedLine]:
    """Parse a KiCad BOM export into collapsed, unsorted lines.

    Raises :class:`NoMpnColumnError` when no header maps to an MPN column.
    """
    if isinstance(data, bytes):
        text = data.decode("utf-8-sig")
    else:
        text = data
    if not text.strip():
        raise BomError("the BOM file is empty")

    delimiter = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows or not any(rows[0]):
        raise BomError("the BOM file has no header row")

    header = rows[0]
    columns = _map_columns(header)
    if "mpn" not in columns.values():
        raise NoMpnColumnError([h for h in header if h.strip()])

    parsed: list[_RawRow] = []
    for number, row in enumerate(rows[1:], start=2):
        parsed_row = _parse_row(row, columns, number)
        if parsed_row is not None:
            parsed.append(parsed_row)

    if not parsed:
        raise BomError("the BOM contains no lines")

    return _collapse(parsed)
