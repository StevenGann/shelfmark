"""KiCad position (``.pos``) parsing for the OpenPnP handoff.

KiCad's position export varies between versions — ``Ref`` or ``Reference``,
``Mid X`` or ``PosX``, ``Side`` or ``Layer`` — so the parser maps header
*aliases* rather than positions, exactly like ``app.bom``. Output is a list of
placement dicts ready for ``repo.builds.replace_placements``.
"""

from __future__ import annotations

import csv

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "reference": ("Ref", "Reference", "Designator"),
    "value": ("Val", "Value", "Comment"),
    "package": ("Package", "Footprint"),
    "x": ("PosX", "Mid X", "X"),
    "y": ("PosY", "Mid Y", "Y"),
    "rotation": ("Rot", "Rotation"),
    "side": ("Side", "Layer"),
}

_DELIMITERS = (",", ";", "\t")


class PositionError(Exception):
    """A position file cannot be parsed.

    ``line`` is the one-based line number in the file, ``text`` the offending
    cell, so the operator can find the bad row without guessing.
    """

    def __init__(self, line: int, text: str) -> None:
        self.line = line
        self.text = text
        super().__init__(f"line {line}: cannot parse coordinate {text!r}")


def _norm_header(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum()).lower()


_ALIAS_BY_NORM: dict[str, str] = {
    _norm_header(alias): field for field, aliases in FIELD_ALIASES.items() for alias in aliases
}


def _map_columns(header: list[str]) -> dict[int, str]:
    columns: dict[int, str] = {}
    for idx, name in enumerate(header):
        field = _ALIAS_BY_NORM.get(_norm_header(name))
        if field is not None:
            columns[idx] = field
    return columns


def _pick_delimiter(line: str) -> str | None:
    """Return the delimiter a header line uses, or None for whitespace columns."""
    for candidate in _DELIMITERS:
        if candidate in line:
            return candidate
    return None


def _normalise_side(raw: str) -> str:
    side = raw.strip().lower()
    if side in ("bottom", "b"):
        return "Bottom"
    return "Top"


def _parse_float(value: str, line: int, field: str) -> float:
    try:
        return float(value.strip())
    except ValueError as exc:
        raise PositionError(line, value.strip()) from exc


def _split_row(tokens: list[str], header: list[str]) -> dict[str, str]:
    """Map one whitespace-aligned row onto the header's fields.

    KiCad pads these columns rather than separating them, and nothing stops a
    value from containing a space ("10k 1%"), so a row with more tokens than the
    header is read from the right: side, rotation, y and x are always the last
    four tokens and the package is the one before them.
    """
    if len(tokens) == len(header):
        return dict(zip(header, tokens, strict=False))
    if len(tokens) < 6:
        raise PositionError(0, "row has too few columns: " + " ".join(tokens))
    return {
        header[0]: tokens[0],
        header[1]: " ".join(tokens[1:-5]),
        header[2]: tokens[-5],
        header[3]: tokens[-4],
        header[4]: tokens[-3],
        header[5]: tokens[-2],
        header[6]: tokens[-1],
    }


def parse_positions(data: str | bytes) -> list[dict]:
    """Parse a KiCad position file into a list of placement dicts.

    Two shapes are accepted, because KiCad writes both: comma/semicolon/tab
    separated with a header row, and the whitespace-aligned layout of the
    default ``.pos`` export (``# Ref  Val  Package  PosX  PosY  Rot  Side``).
    Returns dicts with keys ``reference``, ``side``, ``x_mm``, ``y_mm``,
    ``rotation_deg``, ``package`` and ``value``. A row whose coordinate cannot
    be parsed raises :class:`PositionError` naming the line and the text.
    """
    if isinstance(data, bytes):
        text = data.decode("utf-8-sig")
    else:
        text = data
    if not text.strip():
        raise PositionError(0, "the position file is empty")

    lines = text.splitlines()
    header_at: int | None = None
    header_fields: list[str] = []
    delimiter: str | None = None
    for idx, line in enumerate(lines):
        candidate = line.lstrip("#").strip()
        if not candidate:
            continue
        delimiter = _pick_delimiter(candidate)
        tokens = [
            t.strip() for t in (candidate.split(delimiter) if delimiter else candidate.split())
        ]
        columns = _map_columns(tokens)
        if {"reference", "x", "y"} <= set(columns.values()):
            header_at = idx
            header_fields = tokens
            break
    if header_at is None:
        raise PositionError(
            1,
            "missing columns: reference, x, y — no header row found",
        )

    placements: list[dict] = []
    for number, line in enumerate(lines[header_at + 1 :], start=header_at + 2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if delimiter is None:
            cells = _split_row(stripped.split(), header_fields)
            cells = {_ALIAS_BY_NORM.get(_norm_header(k), k): v for k, v in cells.items()}
        else:
            row = next(csv.reader([line], delimiter=delimiter))
            cells = {
                field: (row[idx].strip() if idx < len(row) else "")
                for idx, field in _map_columns(header_fields).items()
            }
        reference = (cells.get("reference") or "").strip()
        if not reference:
            continue

        x_mm = _parse_float(cells.get("x", ""), number, "x")
        y_mm = _parse_float(cells.get("y", ""), number, "y")

        rotation_raw = cells.get("rotation", "")
        rotation_deg = 0.0
        if rotation_raw:
            try:
                rotation_deg = float(rotation_raw)
            except ValueError as exc:
                raise PositionError(number, rotation_raw) from exc

        placements.append(
            {
                "reference": reference,
                "side": _normalise_side(cells.get("side", "")),
                "x_mm": x_mm,
                "y_mm": y_mm,
                "rotation_deg": rotation_deg,
                "package": cells.get("package") or None,
                "value": cells.get("value") or None,
            }
        )

    if not placements:
        raise PositionError(0, "the position file contains no placements")
    return placements
