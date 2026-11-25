"""Repository functions for builds, build lines, placements and exports.

A build is a view over BOM lines that already exist: planning copies each
non-DNP line into a ``build_line`` with ``required = quantity * boards`` and a
``short`` snapshot. Committing consumes stock through ``stock.apply_move`` inside
one ``BEGIN IMMEDIATE`` transaction, so a shortfall writes nothing.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from . import boms as boms_repo
from . import stock as stock_repo
from . import substitutes as subs_repo


class BuildError(Exception):
    """A build operation cannot proceed."""


class BuildNotFound(BuildError):
    def __init__(self, build_id: int) -> None:
        self.build_id = build_id
        super().__init__(f"build not found: {build_id}")


class BuildNotPlanning(BuildError):
    """The build is committed or abandoned and cannot be changed."""


class BuildShortage(BuildError):
    """A line cannot be covered; carries the operator-facing message."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class PositionsRequired(BuildError):
    """The revision has no placements, so the OpenPnP board file cannot be built."""


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value).strip("-")


def board_file_name(project_name: str, revision_label: str) -> str:
    parts = [s for s in (_slug(project_name), _slug(revision_label)) if s]
    return ("-".join(parts) if parts else "board") + ".board.xml"


# --- builds ------------------------------------------------------------------


def _row_to_build(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "revision_id": int(row["revision_id"]),
        "boards": int(row["boards"]),
        "status": row["status"],
        "note": row["note"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "committed_at": row["committed_at"],
        "revision_label": row["revision_label"],
        "project_id": int(row["project_id"]),
        "project_name": row["project_name"],
    }


_BUILD_SELECT = (
    "SELECT b.*, r.label AS revision_label, r.project_id, p.name AS project_name "
    "FROM build b "
    "JOIN bom_revision r ON r.id = b.revision_id "
    "JOIN project p ON p.id = r.project_id"
)


def _get_build_row(conn: sqlite3.Connection, build_id: int) -> sqlite3.Row | None:
    return conn.execute(f"{_BUILD_SELECT} WHERE b.id = ?", (build_id,)).fetchone()


_LINE_SELECT = (
    "SELECT bl.id, bl.build_id, bl.bom_line_id, bl.part_id, bl.required, bl.consumed, bl.short, "
    "l.mpn AS line_mpn, l.manufacturer AS line_manufacturer, "
    "l.package AS line_package, l.value AS line_value, "
    "l.references_json, l.dnp, l.match_status, "
    "p.ipn, p.mpn AS part_mpn, p.package AS part_package, p.value AS part_value, "
    "m.name AS part_manufacturer "
    "FROM build_line bl "
    "JOIN bom_line l ON l.id = bl.bom_line_id "
    "LEFT JOIN part p ON p.id = bl.part_id "
    "LEFT JOIN manufacturer m ON m.id = p.manufacturer_id"
)


def _substitute_coverage(conn: sqlite3.Connection, part_id: int, short: int) -> list[dict]:
    subs = subs_repo.list_out(conn, part_id)
    for sub in subs:
        sub["covers"] = short > 0 and int(sub["substitute_on_hand"]) >= short
    return subs


def _row_to_build_line(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    part_id = row["part_id"]
    if part_id is not None:
        mpn = row["part_mpn"]
        manufacturer = row["part_manufacturer"]
        package = row["part_package"]
        value = row["part_value"]
        on_hand = stock_repo.total_on_hand(conn, int(part_id))
        substitutes = _substitute_coverage(conn, int(part_id), int(row["short"]))
    else:
        mpn = row["line_mpn"]
        manufacturer = row["line_manufacturer"]
        package = row["line_package"]
        value = row["line_value"]
        on_hand = 0
        substitutes = []
    return {
        "id": int(row["id"]),
        "build_id": int(row["build_id"]),
        "bom_line_id": int(row["bom_line_id"]),
        "part_id": part_id,
        "ipn": row["ipn"],
        "mpn": mpn,
        "manufacturer": manufacturer,
        "package": package,
        "value": value,
        "required": int(row["required"]),
        "consumed": int(row["consumed"]),
        "short": int(row["short"]),
        "on_hand": on_hand,
        "references": json.loads(row["references_json"] or "[]"),
        "dnp": bool(row["dnp"]),
        "match_status": row["match_status"],
        "substitutes": substitutes,
    }


def get_build_lines(conn: sqlite3.Connection, build_id: int) -> list[dict]:
    rows = conn.execute(
        f"{_LINE_SELECT} WHERE bl.build_id = ? ORDER BY bl.id", (build_id,)
    ).fetchall()
    return [_row_to_build_line(conn, r) for r in rows]


def get_build(conn: sqlite3.Connection, build_id: int) -> dict | None:
    row = _get_build_row(conn, build_id)
    if row is None:
        return None
    build = _row_to_build(row)
    build["lines"] = get_build_lines(conn, build_id)
    return build


def list_builds(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"{_BUILD_SELECT} ORDER BY b.id DESC").fetchall()
    builds = []
    for row in rows:
        build = _row_to_build(row)
        build["lines"] = get_build_lines(conn, int(build["id"]))
        builds.append(build)
    return builds


def list_revision_builds(conn: sqlite3.Connection, revision_id: int) -> list[dict]:
    rows = conn.execute(
        f"{_BUILD_SELECT} WHERE b.revision_id = ? ORDER BY b.id DESC", (revision_id,)
    ).fetchall()
    return [_row_to_build(row) for row in rows]


def create_build(
    conn: sqlite3.Connection,
    *,
    revision_id: int,
    boards: int,
    note: str | None,
    created_by: str,
) -> dict:
    boards = int(boards)
    if boards < 1:
        raise ValueError("boards must be at least 1")
    revision = boms_repo.get_revision(conn, revision_id)
    if revision is None:
        raise ValueError(f"revision not found: {revision_id}")

    lines = boms_repo.get_lines(conn, revision_id)
    now = _utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO build (revision_id, boards, status, note, created_by, created_at) "
            "VALUES (?, ?, 'planning', ?, ?, ?)",
            (revision_id, boards, note, created_by, now),
        )
        assert cur.lastrowid is not None
        build_id = int(cur.lastrowid)
        for line in lines:
            if line["dnp"]:
                continue
            part_id = line["part_id"]
            required = int(line["quantity"]) * boards
            on_hand = stock_repo.total_on_hand(conn, int(part_id)) if part_id is not None else 0
            short = max(0, required - on_hand)
            conn.execute(
                "INSERT INTO build_line "
                "(build_id, bom_line_id, part_id, required, consumed, short) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (build_id, int(line["id"]), part_id, required, short),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    build = get_build(conn, build_id)
    assert build is not None
    return build


def _line_label(line: dict) -> str:
    return " ".join(p for p in (line["ipn"], line["package"], line["value"]) if p) or line["mpn"]


def _draw_plan(
    conn: sqlite3.Connection, part_id: int, required: int, preferred: str | None
) -> list[tuple[str, int]]:
    items = stock_repo.get_stock(conn, part_id)
    preferred_norm = stock_repo.location_norm(preferred) if preferred else None

    def sort_key(item: dict) -> tuple[int, int, str, int]:
        return (
            0 if item["location_norm"] == preferred_norm else 1,
            -int(item["quantity"]),
            item["location_norm"],
            int(item["id"]),
        )

    plan: list[tuple[str, int]] = []
    remaining = required
    for item in sorted(items, key=sort_key):
        if remaining <= 0:
            break
        if int(item["quantity"]) <= 0:
            continue
        take = min(int(item["quantity"]), remaining)
        plan.append((item["location"], take))
        remaining -= take
    return plan


def commit_build(
    conn: sqlite3.Connection,
    build_id: int,
    locations: dict[str, str] | None,
    actor: str,
) -> dict:
    build_row = _get_build_row(conn, build_id)
    if build_row is None:
        raise BuildNotFound(build_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _get_build_row(conn, build_id)
        assert current is not None
        if current["status"] != "planning":
            raise BuildNotPlanning(f"build {build_id} is {current['status']}")
        boards = int(current["boards"])
        lines = get_build_lines(conn, build_id)
        locations = locations or {}

        # Pre-flight against the *cumulative* demand: two lines can name the same
        # part, and each would pass a per-line check while the pair cannot be met.
        needed: dict[int, int] = {}
        for line in lines:
            part_id = line["part_id"]
            required = int(line["required"])
            if part_id is None:
                raise BuildShortage(
                    f"{_line_label(line)}: need {required}, have 0 — {required} short"
                )
            needed[int(part_id)] = needed.get(int(part_id), 0) + required
            on_hand = stock_repo.total_on_hand(conn, int(part_id))
            if on_hand < needed[int(part_id)]:
                short = needed[int(part_id)] - on_hand
                raise BuildShortage(
                    f"{_line_label(line)}: need {needed[int(part_id)]}, "
                    f"have {on_hand} — {short} short"
                )

        note = f"build {build_id} of {boards}"
        for line in lines:
            # Drawn now, not before the loop: the moves above have already reduced
            # stock for any part named by an earlier line.
            plan = _draw_plan(
                conn,
                int(line["part_id"]),
                int(line["required"]),
                locations.get(str(line["id"])),
            )
            for location, take in plan:
                stock_repo.apply_move(
                    conn,
                    int(line["part_id"]),
                    location,
                    "build",
                    take,
                    note,
                    actor,
                )
            conn.execute(
                "UPDATE build_line SET consumed = required WHERE id = ?", (int(line["id"]),)
            )
        conn.execute(
            "UPDATE build SET status = 'committed', committed_at = ? WHERE id = ?",
            (_utc_now(), build_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    build = get_build(conn, build_id)
    assert build is not None
    return build


def abandon_build(conn: sqlite3.Connection, build_id: int) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _get_build_row(conn, build_id)
        if current is None:
            raise BuildNotFound(build_id)
        if current["status"] != "planning":
            raise BuildNotPlanning(f"build {build_id} is {current['status']}")
        conn.execute("UPDATE build SET status = 'abandoned' WHERE id = ?", (build_id,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    build = get_build(conn, build_id)
    assert build is not None
    return build


# --- placements --------------------------------------------------------------


def replace_placements(conn: sqlite3.Connection, revision_id: int, placements: list[dict]) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM placement WHERE revision_id = ?", (revision_id,))
        for p in placements:
            conn.execute(
                "INSERT INTO placement "
                "(revision_id, reference, side, x_mm, y_mm, rotation_deg, package, value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    p["reference"],
                    p["side"],
                    p["x_mm"],
                    p["y_mm"],
                    p["rotation_deg"],
                    p["package"],
                    p["value"],
                ),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"count": len(placements)}


def get_placements(conn: sqlite3.Connection, revision_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM placement WHERE revision_id = ? ORDER BY id", (revision_id,)
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "revision_id": int(row["revision_id"]),
            "reference": row["reference"],
            "side": row["side"],
            "x_mm": float(row["x_mm"]),
            "y_mm": float(row["y_mm"]),
            "rotation_deg": float(row["rotation_deg"]),
            "package": row["package"],
            "value": row["value"],
        }
        for row in rows
    ]


# --- exports -----------------------------------------------------------------


def _primary_locations(conn: sqlite3.Connection, part_id: int, required: int) -> list[str]:
    return [location for location, _take in _draw_plan(conn, part_id, required, None)]


def picklist_text(conn: sqlite3.Connection, build_id: int) -> str:
    if _get_build_row(conn, build_id) is None:
        raise BuildNotFound(build_id)
    lines = get_build_lines(conn, build_id)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "ipn",
            "mpn",
            "manufacturer",
            "package",
            "value",
            "required",
            "on_hand",
            "location",
            "substitutes",
        ]
    )
    matched = [line for line in lines if line["part_id"] is not None]
    unmatched = [line for line in lines if line["part_id"] is None]
    for line in matched + unmatched:
        substitutes = "; ".join(f"{s['substitute_ipn']} {s['fit']}" for s in line["substitutes"])
        if line["part_id"] is not None:
            locations = ", ".join(
                _primary_locations(conn, int(line["part_id"]), int(line["required"]))
            )
            writer.writerow(
                [
                    line["ipn"],
                    line["mpn"],
                    line["manufacturer"],
                    line["package"],
                    line["value"],
                    line["required"],
                    line["on_hand"],
                    locations,
                    substitutes,
                ]
            )
        else:
            writer.writerow(
                [
                    "",
                    line["mpn"],
                    line["manufacturer"],
                    line["package"],
                    line["value"],
                    line["required"],
                    0,
                    "",
                    "",
                ]
            )
    return out.getvalue()


def _fmt_number(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text + ".0" if "." not in text else text


def board_xml_text(conn: sqlite3.Connection, build_id: int) -> tuple[str, int]:
    row = _get_build_row(conn, build_id)
    if row is None:
        raise BuildNotFound(build_id)
    revision = boms_repo.get_revision(conn, int(row["revision_id"]))
    assert revision is not None
    project = boms_repo.get_project(conn, int(revision["project_id"]))
    assert project is not None

    placements = get_placements(conn, int(revision["id"]))
    if not placements:
        raise PositionsRequired()

    refmap: dict[str, dict] = {}
    for line in boms_repo.get_lines(conn, int(revision["id"])):
        for ref in line["references"]:
            refmap[ref] = line

    name = board_file_name(str(project["name"]), str(revision["label"]))
    root = ET.Element("openpnp-board", {"name": name})
    ET.SubElement(root, "outline", {"units": "Millimeters"})
    ET.SubElement(root, "fiducials")
    placements_el = ET.SubElement(root, "placements")

    skipped = 0
    for placement in placements:
        mapped = refmap.get(placement["reference"])
        if mapped is None or mapped["dnp"] or mapped["part_id"] is None or not mapped["ipn"]:
            if mapped is None or (not mapped["dnp"]):
                skipped += 1
            continue
        place = ET.SubElement(
            placements_el,
            "placement",
            {
                "version": "1.1",
                "id": placement["reference"],
                "side": placement["side"],
                "part-id": str(mapped["ipn"]),
                "type": "Place",
            },
        )
        ET.SubElement(
            place,
            "location",
            {
                "units": "Millimeters",
                "x": _fmt_number(placement["x_mm"]),
                "y": _fmt_number(placement["y_mm"]),
                "z": "0.0",
                "rotation": _fmt_number(placement["rotation_deg"]),
            },
        )
    ET.indent(root)
    return ET.tostring(root, encoding="unicode"), skipped


def job_xml_text(conn: sqlite3.Connection, build_id: int) -> str:
    row = _get_build_row(conn, build_id)
    if row is None:
        raise BuildNotFound(build_id)
    revision = boms_repo.get_revision(conn, int(row["revision_id"]))
    assert revision is not None
    project = boms_repo.get_project(conn, int(revision["project_id"]))
    assert project is not None

    name = board_file_name(str(project["name"]), str(revision["label"]))
    root = ET.Element("openpnp-job")
    locations_el = ET.SubElement(root, "board-locations")
    for _ in range(int(row["boards"])):
        board_location = ET.SubElement(
            locations_el, "board-location", {"side": "Top", "board-file": name}
        )
        ET.SubElement(
            board_location,
            "location",
            {
                "units": "Millimeters",
                "x": "0.0",
                "y": "0.0",
                "z": "0.0",
                "rotation": "0.0",
            },
        )
    ET.indent(root)
    return ET.tostring(root, encoding="unicode")
