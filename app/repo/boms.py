"""Repository functions for projects, BOM revisions and BOM lines.

Like the other repositories there is no ORM: each function takes a connection
and the write helpers own their transaction. An import writes one revision and
all of its lines in a single transaction, so a failed import leaves no partial
revision. The stored CSV lives under the asset root, written atomically with the
same helpers as part assets.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from .. import assets
from ..bom import ParsedLine, natural_key, parse_bom
from ..mpn import fold_name, normalise_value
from . import parts as parts_repo
from .catalog import normalise_name
from .parts import DuplicatePartError


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class DuplicateProjectError(Exception):
    def __init__(self, project_id: int) -> None:
        self.project_id = project_id
        super().__init__(f"project already exists: {project_id}")


class DuplicateRevisionError(Exception):
    def __init__(self, revision_id: int) -> None:
        self.revision_id = revision_id
        super().__init__(f"revision already imported: {revision_id}")


def _combine_notes(*parts: str | None) -> str | None:
    return "; ".join(p for p in parts if p) or None


# --- projects ---------------------------------------------------------------


def _row_to_project(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "name_norm": row["name_norm"],
        "description": row["description"],
        "created_at": row["created_at"],
    }


def _project_with_counts(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    project_id = int(row["id"])
    newest = conn.execute(
        "SELECT id, label FROM bom_revision WHERE project_id = ? ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    revision_count = int(
        conn.execute(
            "SELECT count(*) AS n FROM bom_revision WHERE project_id = ?", (project_id,)
        ).fetchone()["n"]
    )
    unmatched = 0
    newest_label: str | None = None
    if newest is not None:
        newest_label = newest["label"]
        unmatched = int(
            conn.execute(
                "SELECT count(*) AS n FROM bom_line "
                "WHERE revision_id = ? AND match_status != 'matched'",
                (int(newest["id"]),),
            ).fetchone()["n"]
        )
    project = _row_to_project(row)
    project.update(
        {
            "revision_count": revision_count,
            "newest_label": newest_label,
            "unmatched_count": unmatched,
        }
    )
    return project


def create_project(conn: sqlite3.Connection, *, name: str, description: str | None = None) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    name_norm = normalise_name(name)
    now = _utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM project WHERE name_norm = ?", (name_norm,)
        ).fetchone()
        if existing is not None:
            raise DuplicateProjectError(int(existing["id"]))
        cur = conn.execute(
            "INSERT INTO project (name, name_norm, description, created_at) VALUES (?, ?, ?, ?)",
            (name, name_norm, description, now),
        )
        assert cur.lastrowid is not None
        project_id = int(cur.lastrowid)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    project = get_project(conn, project_id)
    assert project is not None
    return project


def get_project(conn: sqlite3.Connection, project_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    return None if row is None else _project_with_counts(conn, row)


def get_project_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM project WHERE name_norm = ?", (normalise_name(name),)
    ).fetchone()
    return None if row is None else _project_with_counts(conn, row)


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM project ORDER BY name_norm").fetchall()
    return [_project_with_counts(conn, r) for r in rows]


# --- revisions --------------------------------------------------------------


def _row_to_revision(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "project_id": int(row["project_id"]),
        "label": row["label"],
        "source_filename": row["source_filename"],
        "source_sha256": row["source_sha256"],
        "stored_path": row["stored_path"],
        "line_count": int(row["line_count"]),
        "imported_at": row["imported_at"],
        "imported_by": row["imported_by"],
        "note": row["note"],
    }


def get_revision(conn: sqlite3.Connection, revision_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM bom_revision WHERE id = ?", (revision_id,)).fetchone()
    return None if row is None else _row_to_revision(row)


def list_revisions(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM bom_revision WHERE project_id = ? ORDER BY id DESC", (project_id,)
    ).fetchall()
    return [_row_to_revision(r) for r in rows]


def _newest_revision(conn: sqlite3.Connection, project_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM bom_revision WHERE project_id = ? ORDER BY id DESC LIMIT 1", (project_id,)
    ).fetchone()
    return None if row is None else _row_to_revision(row)


def _apply_package_update(conn: sqlite3.Connection, part_id: int, package: str) -> None:
    """Set a part's package to the KiCad footprint value (footprint wins). No commit."""
    conn.execute(
        "UPDATE part SET package = ?, updated_at = ? WHERE id = ?",
        (package, _utc_now(), part_id),
    )


def _fill_part_value(conn: sqlite3.Connection, part_id: int, value: str) -> None:
    """Set a part's value from a BOM line when it has none. Never overwrites.

    ``value_norm`` is derived with the same fold (case, spaces, the micro sign)
    the substitution suggestions use, so "2.2uF" and "2.2 µF" are one value.
    """
    row = conn.execute("SELECT value FROM part WHERE id = ?", (part_id,)).fetchone()
    if row is not None and not (row["value"] and str(row["value"]).strip()):
        conn.execute(
            "UPDATE part SET value = ?, value_norm = ?, updated_at = ? WHERE id = ?",
            (value, normalise_value(value), _utc_now(), part_id),
        )


def create_revision(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    label: str,
    source_filename: str,
    source_sha256: str,
    stored_path: str | None,
    imported_by: str,
    note: str | None,
    lines: list[dict],
) -> dict:
    """Insert a revision and its (already matched) lines in one transaction.

    ``lines`` are the dicts produced by :func:`match_lines`; the optional
    ``package_update`` on a matched line is applied to the part here, inside the
    same transaction, so a failure rolls back the footprint change too.
    """
    label = label.strip()
    if not label:
        raise ValueError("label is required")
    now = _utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO bom_revision "
            "(project_id, label, source_filename, source_sha256, stored_path, "
            "line_count, imported_at, imported_by, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                label,
                source_filename,
                source_sha256,
                stored_path,
                len(lines),
                now,
                imported_by,
                note,
            ),
        )
        assert cur.lastrowid is not None
        revision_id = int(cur.lastrowid)
        for line in lines:
            conn.execute(
                "INSERT INTO bom_line "
                "(revision_id, part_id, mpn, mpn_norm, manufacturer, manufacturer_norm, "
                "description, value, footprint, package, quantity, references_json, dnp, "
                "match_status, match_note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    line["part_id"],
                    line["mpn"],
                    line["mpn_norm"],
                    line["manufacturer"],
                    line["manufacturer_norm"],
                    line["description"],
                    line["value"],
                    line["footprint"],
                    line["package"],
                    line["quantity"],
                    json.dumps(line["references"]),
                    int(line["dnp"]),
                    line["match_status"],
                    line["match_note"],
                    now,
                ),
            )
            if line.get("package_update"):
                _apply_package_update(conn, int(line["part_id"]), str(line["package_update"]))
            if line.get("part_id") and line.get("value"):
                _fill_part_value(conn, int(line["part_id"]), str(line["value"]))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    revision = get_revision(conn, revision_id)
    assert revision is not None
    return revision


# --- lines ------------------------------------------------------------------


def _row_to_line(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "revision_id": int(row["revision_id"]),
        "part_id": row["part_id"],
        "ipn": row["ipn"],
        "mpn": row["mpn"],
        "mpn_norm": row["mpn_norm"],
        "manufacturer": row["manufacturer"],
        "manufacturer_norm": row["manufacturer_norm"],
        "description": row["description"],
        "value": row["value"],
        "footprint": row["footprint"],
        "package": row["package"],
        "quantity": int(row["quantity"]),
        "references": json.loads(row["references_json"] or "[]"),
        "dnp": bool(row["dnp"]),
        "match_status": row["match_status"],
        "match_note": row["match_note"],
        "created_at": row["created_at"],
    }


def get_lines(conn: sqlite3.Connection, revision_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT l.*, p.ipn AS ipn FROM bom_line l "
        "LEFT JOIN part p ON p.id = l.part_id WHERE l.revision_id = ? ORDER BY l.id",
        (revision_id,),
    ).fetchall()
    return [_row_to_line(r) for r in rows]


def get_line(conn: sqlite3.Connection, line_id: int) -> dict | None:
    row = conn.execute(
        "SELECT l.*, p.ipn AS ipn FROM bom_line l "
        "LEFT JOIN part p ON p.id = l.part_id WHERE l.id = ?",
        (line_id,),
    ).fetchone()
    return None if row is None else _row_to_line(row)


# --- matching ---------------------------------------------------------------


def match_lines(conn: sqlite3.Connection, parsed: list[ParsedLine]) -> list[dict]:
    """Match each parsed line against the parts catalogue.

    A line matches on ``(mpn_norm, manufacturer_norm)``. Without a manufacturer
    it matches on ``mpn_norm`` alone, and only when exactly one part carries it
    — otherwise it is ``ambiguous``. The KiCad footprint wins over the supplier
    package string: when a matched part's package is empty or differs, the line
    carries a ``package_update`` that :func:`create_revision` applies.
    """
    out: list[dict] = []
    for line in parsed:
        # Candidate parts by MPN first: the MPN is the strong key, and the
        # manufacturer is compared afterwards so that spelling differences
        # ("Würth" vs "Wurth") do not hide a part that is plainly there.
        candidates = conn.execute(
            "SELECT p.id, p.ipn, p.package, m.name AS manufacturer_name FROM part p "
            "JOIN manufacturer m ON m.id = p.manufacturer_id "
            "WHERE p.mpn_norm = ?",
            (line.mpn_norm,),
        ).fetchall()
        if line.manufacturer_norm:
            wanted = fold_name(line.manufacturer or "")
            exact = [
                row
                for row in candidates
                if str(row["manufacturer_name"]).strip().lower()
                == (line.manufacturer or "").strip().lower()
            ]
            folded = [
                row for row in candidates if fold_name(str(row["manufacturer_name"])) == wanted
            ]
            # Exact spelling first: if the catalogue has one part under the very
            # name the BOM used, that is the answer even when another row spells
            # the same company differently (a duplicate the catalogue should
            # lose). Folding is the fallback, not the first rule.
            if len(exact) == 1:
                rows = exact
            elif len(folded) == 1:
                rows = folded
            elif folded:
                rows = folded
            else:
                rows = []
        else:
            rows = candidates

        part_id: int | None = None
        status = "unmatched"
        notes: list[str] = []
        package_update: str | None = None

        if len(rows) == 1:
            status = "matched"
            part_id = int(rows[0]["id"])
        elif len(rows) > 1:
            status = "ambiguous"
            ipns = ", ".join(sorted(r["ipn"] for r in rows))
            notes.append(f"MPN matches {len(rows)} parts ({ipns})")

        if status == "unmatched":
            if candidates:
                others = ", ".join(
                    sorted(f"{row['ipn']} ({row['manufacturer_name']})" for row in candidates)
                )
                notes.append(f"MPN exists under a different manufacturer: {others}")
            elif line.manufacturer_norm:
                notes.append(
                    f"no part with MPN {line.mpn_norm} and manufacturer {line.manufacturer_norm}"
                )
            else:
                notes.append(f"no part with MPN {line.mpn_norm}")

        match_note = _combine_notes(line.note, "; ".join(notes) or None)

        if status == "matched" and line.package:
            current_package = rows[0]["package"]
            if not current_package or current_package != line.package:
                package_update = line.package
                was = current_package or "empty"
                match_note = _combine_notes(
                    match_note, f"package updated to {line.package} (was {was})"
                )

        out.append(
            {
                "mpn": line.mpn,
                "mpn_norm": line.mpn_norm,
                "manufacturer": line.manufacturer,
                "manufacturer_norm": line.manufacturer_norm,
                "description": line.description,
                "value": line.value,
                "footprint": line.footprint,
                "package": line.package,
                "quantity": line.quantity,
                "references": line.references,
                "dnp": line.dnp,
                "match_status": status,
                "match_note": match_note,
                "part_id": part_id,
                "package_update": package_update,
            }
        )
    return out


# --- diff -------------------------------------------------------------------


def _line_key(line: dict) -> tuple[str, str]:
    return (line["mpn_norm"], line["manufacturer_norm"] or "")


def _line_summary(line: dict) -> dict:
    return {"mpn": line["mpn"], "quantity": line["quantity"], "references": line["references"]}


def _changed(previous: dict, current: dict) -> bool:
    return (
        previous["quantity"] != current["quantity"]
        or previous["references"] != current["references"]
    )


def _change_entry(previous: dict, current: dict) -> dict:
    prev_refs = set(previous["references"])
    curr_refs = set(current["references"])
    added = sorted(curr_refs - prev_refs, key=lambda r: (natural_key(r), r))
    removed = sorted(prev_refs - curr_refs, key=lambda r: (natural_key(r), r))
    return {
        "mpn": current["mpn"],
        "from": {"quantity": previous["quantity"]},
        "to": {"quantity": current["quantity"]},
        "references_added": added,
        "references_removed": removed,
    }


def _diff(previous: list[dict], current: list[dict]) -> dict:
    prev_by_key = {_line_key(line): line for line in previous}
    curr_by_key = {_line_key(line): line for line in current}

    added: list[dict] = []
    removed: list[dict] = []
    changed: list[dict] = []
    unchanged = 0

    for key in sorted(curr_by_key):
        current_line = curr_by_key[key]
        previous_line = prev_by_key.get(key)
        if previous_line is None:
            added.append(_line_summary(current_line))
        elif _changed(previous_line, current_line):
            changed.append(_change_entry(previous_line, current_line))
        else:
            unchanged += 1

    for key in sorted(prev_by_key):
        if key not in curr_by_key:
            removed.append(_line_summary(prev_by_key[key]))

    unmatched = [
        {"mpn": line["mpn"], "manufacturer": line["manufacturer"], "quantity": line["quantity"]}
        for line in current
        if line["match_status"] != "matched"
    ]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": unchanged,
        "unmatched": unmatched,
    }


def diff(conn: sqlite3.Connection, previous: int | None, current: int) -> dict:
    """Diff two revisions keyed by ``(mpn_norm, manufacturer_norm)``.

    ``previous`` may be ``None``, in which case every line of ``current`` is
    ``added``.
    """
    prev_lines = get_lines(conn, previous) if previous is not None else []
    curr_lines = get_lines(conn, current)
    return _diff(prev_lines, curr_lines)


# --- resolution -------------------------------------------------------------


def _resolve_line(
    conn: sqlite3.Connection,
    line_id: int,
    part_id: int,
    ipn: str,
    package: str | None,
    prior_note: str | None,
    verb: str,
) -> None:
    note = _combine_notes(prior_note, f"{verb} {ipn}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if package is not None:
            current = conn.execute("SELECT package FROM part WHERE id = ?", (part_id,)).fetchone()
            if current is not None and current["package"] != package:
                conn.execute(
                    "UPDATE part SET package = ?, updated_at = ? WHERE id = ?",
                    (package, _utc_now(), part_id),
                )
                note = _combine_notes(note, f"package updated to {package}")
        conn.execute(
            "UPDATE bom_line SET part_id = ?, match_status = 'matched', match_note = ? "
            "WHERE id = ?",
            (part_id, note, line_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def resolve_line(conn: sqlite3.Connection, line_id: int, ipn: str) -> dict:
    """Attach a line to an existing part by IPN."""
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise ValueError(f"no part with IPN {ipn}")
    line = get_line(conn, line_id)
    if line is None:
        raise ValueError(f"no BOM line {line_id}")
    _resolve_line(conn, line_id, int(part["id"]), ipn, None, line["match_note"], "resolved to")
    result = get_line(conn, line_id)
    assert result is not None
    return result


def resolve_unmatched(conn: sqlite3.Connection, revision_id: int) -> list[dict]:
    """Create a part for every unmatched line that carries a manufacturer.

    Parts are created through ``app.repo.parts`` so IPN allocation and MPN
    normalisation behave exactly as they do for a hand-created part. Lines with
    no manufacturer (or an ambiguous match) are left alone.
    """
    created: list[dict] = []
    for line in get_lines(conn, revision_id):
        if line["match_status"] != "unmatched":
            continue
        manufacturer = line["manufacturer"]
        if not manufacturer:
            continue
        try:
            part = parts_repo.create_part(
                conn,
                mpn=line["mpn"],
                manufacturer=manufacturer,
                description=line["description"],
            )
        except DuplicatePartError as exc:
            existing_part = parts_repo.get_part(conn, exc.ipn)
            assert existing_part is not None
            part = existing_part
        _resolve_line(
            conn,
            int(line["id"]),
            int(part["id"]),
            str(part["ipn"]),
            line["package"],
            line["match_note"],
            "created",
        )
        created.append(part)
    return created


# --- import -----------------------------------------------------------------


def _project_slug(name: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    return slug or "project"


def bom_rel_path(project_name: str, sha256: str) -> str:
    """The asset-root-relative path for a stored BOM: ``boms/<project>/<sha256>.csv``."""
    return f"boms/{_project_slug(project_name)}/{sha256}.csv"


def _remove_stored_file(rel_path: str) -> None:
    path = assets.resolve(rel_path)
    if path is not None:
        path.unlink(missing_ok=True)


def import_bom(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    label: str,
    source_filename: str,
    data: bytes,
    imported_by: str,
    note: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Parse, match and diff a BOM, optionally committing it as a new revision.

    ``dry_run`` writes nothing — no revision, no lines, no file, no footprint
    changes. A byte-identical re-import raises :class:`DuplicateRevisionError`.
    The response is the diff shape from the spec, with ``revision_id`` set only
    on a real import.
    """
    parsed = parse_bom(data)
    lines = match_lines(conn, parsed)
    sha256 = hashlib.sha256(data).hexdigest()
    newest = _newest_revision(conn, project_id)
    previous_lines = get_lines(conn, newest["id"]) if newest is not None else []
    diff_result = _diff(previous_lines, lines)
    line_count = len(lines)

    previous_summary = None
    if newest is not None:
        previous_summary = {"label": newest["label"], "imported_at": newest["imported_at"]}

    if dry_run:
        return {
            "dry_run": True,
            "revision_id": None,
            "revision": {"label": label, "line_count": line_count, "imported_at": None},
            "previous": previous_summary,
            **diff_result,
        }

    existing = conn.execute(
        "SELECT id FROM bom_revision WHERE project_id = ? AND source_sha256 = ?",
        (project_id, sha256),
    ).fetchone()
    if existing is not None:
        raise DuplicateRevisionError(int(existing["id"]))

    project = get_project(conn, project_id)
    assert project is not None
    rel_path = bom_rel_path(str(project["name"]), sha256)
    tmp = assets.tmp_path(rel_path)
    tmp.write_bytes(data)
    try:
        assets.commit(tmp, rel_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    try:
        revision = create_revision(
            conn,
            project_id=project_id,
            label=label,
            source_filename=source_filename,
            source_sha256=sha256,
            stored_path=rel_path,
            imported_by=imported_by,
            note=note,
            lines=lines,
        )
    except BaseException:
        _remove_stored_file(rel_path)
        raise

    return {
        "dry_run": False,
        "revision_id": revision["id"],
        "revision": {
            "label": label,
            "line_count": line_count,
            "imported_at": revision["imported_at"],
        },
        "previous": previous_summary,
        **diff_result,
    }
