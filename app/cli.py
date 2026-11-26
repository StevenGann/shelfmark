"""Command-line interface for operators who cannot reach the UI."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import archive, auth, db, enrich, labels, pricing, sessions, users
from .bom import BomError
from .repo import boms as boms_repo
from .repo import builds as builds_repo
from .repo import catalog
from .repo import parts as repo
from .repo import stock as stock_repo
from .repo import substitutes as subs_repo
from .repo.boms import DuplicateRevisionError
from .repo.builds import BuildNotPlanning, BuildShortage, PositionsRequired
from .repo.parts import DuplicatePartError
from .repo.substitutes import FIT_LEVELS, DuplicateSubstituteError


def _print_key_row(row: dict) -> None:
    revoked = row.get("revoked_at") or "-"
    last = row.get("last_used_at") or "-"
    print(
        f"{row['id']}\t{row['name']}\t{row['prefix']}\t{row['role']}\t{row['created_at']}\t{last}\t{revoked}"
    )


def _needs_queue(conn, part_id: int, force: bool) -> bool:
    if force:
        return True
    if catalog.has_pending_job(conn, part_id):
        return False
    latest = catalog.latest_job(conn, part_id)
    return latest is None or latest["status"] != "done"


def _run_export(args: argparse.Namespace) -> int:
    db.migrate()
    conn = db.connect()
    try:
        dest = Path(args.out)
        manifest = archive.export_bundle(dest, conn, assets=not args.no_assets)
        print(f"{dest} {len(manifest['counts'])} tables, {dest.stat().st_size} bytes")
        return 0
    except (OSError, archive.ArchiveError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        conn.close()


def _run_restore(args: argparse.Namespace) -> int:
    db.migrate()
    conn = db.connect()
    try:
        result = archive.restore_bundle(
            Path(args.file), conn, force=args.force, assets=not args.no_assets
        )
    except archive.ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        conn.close()
    for name, count in result["tables"].items():
        print(f"{name} {count} ok")
    print(f"restored {len(result['tables'])} tables, {result['assets']} assets")
    return 0


def _run_backup(args: argparse.Namespace) -> int:
    db.migrate()
    conn = db.connect()
    try:
        path = archive.backup_dir(Path(args.dir), conn, keep=args.keep, assets=not args.no_assets)
    except archive.ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(path)
    return 0


def _run_enrich(args: argparse.Namespace) -> int:
    db.migrate()
    conn = db.connect()
    try:
        if args.all:
            rows = conn.execute("SELECT id, ipn FROM part ORDER BY ipn").fetchall()
            for row in rows:
                if _needs_queue(conn, int(row["id"]), args.force):
                    catalog.queue_job(conn, int(row["id"]))
        else:
            part = repo.get_part(conn, args.ipn)
            if part is None:
                print(f"no such part: {args.ipn}", file=sys.stderr)
                return 1
            if _needs_queue(conn, int(part["id"]), args.force):
                catalog.queue_job(conn, int(part["id"]))
        processed = enrich.run_queue(conn)
        print(f"processed {processed} enrichment jobs")
    finally:
        conn.close()
    return 0


def _read_password() -> str:
    """Read a new password from a terminal, or from stdin when piped.

    The password is never taken from argv: an argument is visible in ``ps`` and
    lands in shell history.
    """
    if sys.stdin.isatty():
        first = getpass.getpass("New password: ")
        second = getpass.getpass("Repeat: ")
        if first != second:
            raise ValueError("Passwords do not match.")
        return first
    return sys.stdin.readline().rstrip("\n")


def _print_user_row(row: dict) -> None:
    state = "disabled" if row.get("disabled_at") else "active"
    last = row.get("last_login_at") or "-"
    print(f"{row['id']}\t{row['username']}\t{row['role']}\t{state}\t{row['created_at']}\t{last}")


def _find_user(conn, username: str):
    return users.get_user_by_username(conn, username)


def _enabled_admins(conn) -> list[dict]:
    return [u for u in users.list_users(conn) if u["role"] == "admin" and not u["disabled_at"]]


def _run_user(args: argparse.Namespace) -> int:
    db.migrate()
    conn = db.connect()
    try:
        if args.user_command == "list":
            for row in users.list_users(conn):
                _print_user_row(row)
            return 0

        if args.user_command == "create":
            password = _read_password()
            try:
                created = users.create_user(conn, args.username, password, args.role)
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 1
            print(f"created {created['username']} ({created['role']})")
            return 0

        user = _find_user(conn, args.username)
        if user is None:
            print(f"no such user: {args.username}", file=sys.stderr)
            return 1

        if args.user_command == "passwd":
            password = _read_password()
            try:
                users.set_password(conn, int(user["id"]), password)
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 1
            # A reset is also a lock-out for whoever was already holding a
            # session with the old password.
            sessions.end_all_for_user(conn, int(user["id"]))
            print(f"password changed for {user['username']}; sessions ended")
            return 0

        if args.user_command == "disable":
            if (
                user["role"] == "admin"
                and not user["disabled_at"]
                and len(_enabled_admins(conn)) <= 1
            ):
                print("refusing to disable the last active admin", file=sys.stderr)
                return 1
            users.set_disabled(conn, int(user["id"]), True)
            sessions.end_all_for_user(conn, int(user["id"]))
            print(f"disabled {user['username']}")
            return 0

        if args.user_command == "enable":
            users.set_disabled(conn, int(user["id"]), False)
            print(f"enabled {user['username']}")
            return 0

        if args.user_command == "role":
            if user["role"] == "admin" and args.role != "admin" and len(_enabled_admins(conn)) <= 1:
                print("refusing to demote the last active admin", file=sys.stderr)
                return 1
            users.set_role(conn, int(user["id"]), args.role)
            print(f"{user['username']} is now {args.role}")
            return 0
    finally:
        conn.close()
    return 1


def _run_bom(args: argparse.Namespace) -> int:
    db.migrate()
    conn = db.connect()
    try:
        if args.bom_command == "list":
            project = boms_repo.get_project_by_name(conn, args.project)
            if project is None:
                print(f"no such project: {args.project}", file=sys.stderr)
                return 1
            for rev in boms_repo.list_revisions(conn, project["id"]):
                lines = boms_repo.get_lines(conn, rev["id"])
                unmatched = sum(1 for line in lines if line["match_status"] != "matched")
                print(
                    f"{rev['id']}\t{rev['label']}\t{rev['line_count']} lines"
                    f"\t{unmatched} unmatched\t{rev['imported_at']}\t{rev['source_filename']}"
                )
            return 0

        project = boms_repo.get_project_by_name(conn, args.project)
        if project is None:
            project = boms_repo.create_project(conn, name=args.project)
        data = open(args.file, "rb").read()
        try:
            result = boms_repo.import_bom(
                conn,
                project_id=project["id"],
                label=args.label,
                source_filename=args.file,
                data=data,
                imported_by="cli",
                note=args.note,
                dry_run=args.dry_run,
            )
        except DuplicateRevisionError as error:
            print(f"already imported as revision {error.revision_id}", file=sys.stderr)
            return 1
        except (BomError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        if args.dry_run:
            print(json.dumps(result))
        else:
            print(
                f"imported revision {result['revision_id']} ({result['revision']['label']}), "
                f"{result['revision']['line_count']} lines"
            )
            print(
                f"added {len(result['added'])}, removed {len(result['removed'])}, "
                f"changed {len(result['changed'])}, unmatched {len(result['unmatched'])}"
            )
        return 0
    finally:
        conn.close()


def _run_label(args: argparse.Namespace) -> int:
    db.migrate()
    try:
        data = open(args.file, "rb").read()
    except OSError as error:
        print(f"cannot read {args.file}: {error}", file=sys.stderr)
        return 1
    try:
        payloads, chosen = labels.decode(data)
    except labels.LabelError as error:
        print(str(error), file=sys.stderr)
        return 1
    parsed = labels.parse_label(chosen, payloads)
    conn = db.connect()
    try:
        resolution = labels.resolve_label(conn, parsed)
        if args.label_command == "scan":
            print(json.dumps({"payloads": payloads, "label": parsed, "resolution": resolution}))
            return 0

        part = resolution["part"]
        if part is None:
            if not args.manufacturer:
                print(
                    "label does not match an existing part; pass --manufacturer to create one",
                    file=sys.stderr,
                )
                return 1
            try:
                part = labels.create_part_from_label(conn, parsed, args.manufacturer)
            except (DuplicatePartError, ValueError) as error:
                print(str(error), file=sys.stderr)
                return 1

        qty = args.quantity if args.quantity is not None else (parsed.get("quantity") or 1)
        note = "; ".join(p for p in (labels.lot_note(parsed), args.note) if p) or None
        try:
            move = stock_repo.record_move(
                conn, int(part["id"]), args.location, "receive", int(qty), note, "cli"
            )
        except (stock_repo.StockError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps({"part": part, "move": move}))
        return 0
    finally:
        conn.close()


def _needs_offer_refresh(conn, part_id: int, stale_days: int) -> bool:
    """True when a part has no digikey ladder fetched within ``stale_days``."""
    cutoff = (datetime.now(UTC) - timedelta(days=stale_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        "SELECT MAX(fetched_at) AS latest FROM offer WHERE part_id = ? AND source = 'digikey'",
        (part_id,),
    ).fetchone()
    latest = row["latest"]
    if latest is None:
        return True
    return latest < cutoff


def _audit_offers(conn) -> int:
    findings = pricing.audit_inversions(conn)
    if not findings:
        print("no inverted price breaks")
        return 0
    for finding in findings:
        b1 = finding["break1"]
        b2 = finding["break2"]
        print(
            f"{finding['ipn']}\t{finding['mpn']}\t{finding['distributor']}\t{finding['sku']}\t"
            f"{b1['quantity']} @ {pricing.format_money(b1['unit_price_micros'])} -> "
            f"{b2['quantity']} @ {pricing.format_money(b2['unit_price_micros'])}"
        )
    return 0


def _refresh_offers(conn, args: argparse.Namespace) -> int:
    if args.ipn:
        part = repo.get_part(conn, args.ipn)
        if part is None:
            print(f"no such part: {args.ipn}", file=sys.stderr)
            return 1
        try:
            pricing.refresh_part_offers(conn, part)
        except Exception as error:  # noqa: BLE001 - report the supplier's message
            print(f"refresh failed for {args.ipn}: {error}", file=sys.stderr)
            return 1
        print(f"refreshed {part['ipn']} ({part['mpn']})")
        return 0

    rows = conn.execute("SELECT id, ipn FROM part ORDER BY ipn").fetchall()
    refreshed = 0
    for row in rows:
        part_id = int(row["id"])
        if not _needs_offer_refresh(conn, part_id, args.stale_days):
            continue
        part = repo.get_part_by_id(conn, part_id)
        if part is None:
            continue
        try:
            pricing.refresh_part_offers(conn, part)
            refreshed += 1
            print(f"refreshed {part['ipn']} ({part['mpn']})")
        except Exception as error:  # noqa: BLE001 - one bad part must not stop the run
            print(f"SKIPPED {row['ipn']}: {error}", file=sys.stderr)
    print(f"refreshed {refreshed} parts")
    return 0


def _run_offers(args: argparse.Namespace) -> int:
    db.migrate()
    conn = db.connect()
    try:
        if args.offers_command == "audit":
            return _audit_offers(conn)
        return _refresh_offers(conn, args)
    finally:
        conn.close()


def _run_subs(args: argparse.Namespace) -> int:
    db.migrate()
    conn = db.connect()
    try:
        part = repo.get_part(conn, args.ipn)
        if part is None:
            print(f"no such part: {args.ipn}", file=sys.stderr)
            return 1

        if args.subs_command == "list":
            for link in subs_repo.list_out(conn, int(part["id"])):
                print(
                    f"{link['substitute_ipn']}\t{link['fit']}\t{link['substitute_mpn']}\t"
                    f"{link['substitute_on_hand']}\t"
                    f"{link['created_by'] or '-'}\t{link['note'] or ''}"
                )
            return 0

        substitute = subs_repo.resolve_part_ref(conn, args.substitute)
        if substitute is None:
            print(f"no such part: {args.substitute}", file=sys.stderr)
            return 1

        if args.subs_command == "add":
            try:
                link = subs_repo.add_link(
                    conn,
                    int(part["id"]),
                    int(substitute["id"]),
                    args.fit,
                    args.note,
                    "cli",
                )
            except DuplicateSubstituteError:
                print(f"link {part['ipn']} -> {substitute['ipn']} already exists", file=sys.stderr)
                return 1
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 1
            print(f"linked {link['part_ipn']} -> {link['substitute_ipn']} ({link['fit']})")
            return 0

        if not subs_repo.remove_link(conn, int(part["id"]), int(substitute["id"])):
            print(f"no link {part['ipn']} -> {substitute['ipn']}", file=sys.stderr)
            return 1
        print(f"removed {part['ipn']} -> {substitute['ipn']}")
        return 0
    finally:
        conn.close()


def _run_build(args: argparse.Namespace) -> int:
    db.migrate()
    conn = db.connect()
    try:
        if args.build_command == "list":
            for build in builds_repo.list_builds(conn):
                print(
                    f"{build['id']}\t{build['project_name']}\t{build['revision_label']}\t"
                    f"{build['boards']} boards\t{build['status']}\t{build['created_at']}"
                )
            return 0

        if args.build_command == "plan":
            try:
                build = builds_repo.create_build(
                    conn,
                    revision_id=args.revision_id,
                    boards=args.boards,
                    note=args.note,
                    created_by="cli",
                )
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 1
            print(f"planned build {build['id']} ({build['boards']} boards)")
            for line in build["lines"]:
                ipn = line["ipn"] or "(unmatched)"
                print(
                    f"  {ipn}\t{line['mpn']}\trequired {line['required']}\t"
                    f"on hand {line['on_hand']}\tshort {line['short']}"
                )
            return 0

        if args.build_command == "commit":
            try:
                build = builds_repo.commit_build(conn, args.build_id, None, "cli")
            except BuildShortage as error:
                print(f"cannot commit: {error.message}", file=sys.stderr)
                return 1
            except BuildNotPlanning as error:
                print(str(error), file=sys.stderr)
                return 1
            print(f"committed build {build['id']} ({build['boards']} boards)")
            return 0

        if args.build_command == "export":
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / "picklist.csv").write_text(builds_repo.picklist_text(conn, args.build_id))
            try:
                board, _skipped = builds_repo.board_xml_text(conn, args.build_id)
            except PositionsRequired:
                print("no positions; board.xml not written", file=sys.stderr)
            else:
                (out / "board.xml").write_text(board)
            (out / "job.xml").write_text(builds_repo.job_xml_text(conn, args.build_id))
            print(f"exported build {args.build_id} to {out}")
            return 0
    finally:
        conn.close()
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shelfmark")
    sub = parser.add_subparsers(dest="command", required=True)

    key = sub.add_parser("key", help="manage API keys")
    key_sub = key.add_subparsers(dest="key_command", required=True)

    create = key_sub.add_parser("create", help="create a key")
    create.add_argument("--name", required=True)
    create.add_argument("--role", required=True, choices=auth.ROLES)

    key_sub.add_parser("list", help="list keys")

    revoke = key_sub.add_parser("revoke", help="revoke a key")
    revoke.add_argument("--name", required=True)

    user = sub.add_parser("user", help="manage human accounts")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    user_sub.add_parser("list", help="list accounts")
    user_create = user_sub.add_parser("create", help="create an account")
    user_create.add_argument("username")
    user_create.add_argument("--role", required=True, choices=users.ROLE_ORDER)
    for name, help_text in (
        ("passwd", "set a password (reads stdin)"),
        ("disable", "disable an account"),
        ("enable", "re-enable an account"),
    ):
        parser_ = user_sub.add_parser(name, help=help_text)
        parser_.add_argument("username")
    user_role = user_sub.add_parser("role", help="change a role")
    user_role.add_argument("username")
    user_role.add_argument("role", choices=users.ROLE_ORDER)

    enrich_parser = sub.add_parser("enrich", help="run the enrichment queue")
    enrich_target = enrich_parser.add_mutually_exclusive_group(required=True)
    enrich_target.add_argument("--all", action="store_true", help="queue every part")
    enrich_target.add_argument("--ipn", metavar="IPN", help="queue a single part")
    enrich_parser.add_argument("--force", action="store_true", help="re-run parts already enriched")

    bom = sub.add_parser("bom", help="import and list BOM revisions")
    bom_sub = bom.add_subparsers(dest="bom_command", required=True)
    bom_import = bom_sub.add_parser("import", help="import a KiCad BOM CSV")
    bom_import.add_argument("--project", required=True)
    bom_import.add_argument("--file", required=True)
    bom_import.add_argument("--label", required=True)
    bom_import.add_argument("--note")
    bom_import.add_argument("--dry-run", action="store_true")
    bom_list = bom_sub.add_parser("list", help="list a project's revisions")
    bom_list.add_argument("--project", required=True)

    label = sub.add_parser(
        "label", help="decode and receive supplier shipping labels (DigiKey, Mouser)"
    )
    label_sub = label.add_subparsers(dest="label_command", required=True)
    label_scan = label_sub.add_parser("scan", help="decode a label image")
    label_scan.add_argument("--file", required=True)
    label_receive = label_sub.add_parser("receive", help="receive stock from a label image")
    label_receive.add_argument("--file", required=True)
    label_receive.add_argument("--location", required=True)
    label_receive.add_argument("--quantity", type=int)
    label_receive.add_argument("--note")
    label_receive.add_argument("--manufacturer")

    offers = sub.add_parser("offers", help="manage offers and price breaks")
    offers_sub = offers.add_subparsers(dest="offers_command", required=True)
    offers_refresh = offers_sub.add_parser("refresh", help="refresh offers from DigiKey")
    offers_target = offers_refresh.add_mutually_exclusive_group(required=True)
    offers_target.add_argument("--ipn", metavar="IPN", help="refresh a single part")
    offers_target.add_argument("--all", action="store_true", help="refresh every stale part")
    offers_refresh.add_argument(
        "--stale-days", type=int, default=7, help="skip parts fetched more recently"
    )
    offers_sub.add_parser("audit", help="report inverted price ladders")

    subs = sub.add_parser("subs", help="manage part substitutions")
    subs_sub = subs.add_subparsers(dest="subs_command", required=True)
    subs_list = subs_sub.add_parser("list", help="list a part's substitutes")
    subs_list.add_argument("ipn")
    subs_add = subs_sub.add_parser("add", help="add a substitute link")
    subs_add.add_argument("ipn")
    subs_add.add_argument("substitute")
    subs_add.add_argument("--fit", required=True, choices=FIT_LEVELS)
    subs_add.add_argument("--note")
    subs_remove = subs_sub.add_parser("remove", help="remove a substitute link")
    subs_remove.add_argument("ipn")
    subs_remove.add_argument("substitute")

    build = sub.add_parser("build", help="plan, commit, list and export builds")
    build_sub = build.add_subparsers(dest="build_command", required=True)
    build_plan = build_sub.add_parser("plan", help="plan a build")
    build_plan.add_argument("revision_id", type=int)
    build_plan.add_argument("--boards", type=int, required=True)
    build_plan.add_argument("--note")
    build_commit = build_sub.add_parser("commit", help="commit a build")
    build_commit.add_argument("build_id", type=int)
    build_sub.add_parser("list", help="list builds")
    build_export = build_sub.add_parser("export", help="export a build's files")
    build_export.add_argument("build_id", type=int)
    build_export.add_argument("--out", required=True)

    export_parser = sub.add_parser("export", help="write a portable backup bundle")
    export_parser.add_argument("--out", required=True)
    export_parser.add_argument("--no-assets", action="store_true")

    restore_parser = sub.add_parser("restore", help="load a bundle into this database")
    restore_parser.add_argument("file")
    restore_parser.add_argument("--force", action="store_true")
    restore_parser.add_argument("--no-assets", action="store_true")

    backup_parser = sub.add_parser("backup", help="write a bundle and prune older ones")
    backup_parser.add_argument("--dir", required=True)
    backup_parser.add_argument("--keep", type=int, default=14)
    backup_parser.add_argument("--no-assets", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "export":
        return _run_export(args)

    if args.command == "restore":
        return _run_restore(args)

    if args.command == "backup":
        return _run_backup(args)

    if args.command == "enrich":
        return _run_enrich(args)

    if args.command == "bom":
        return _run_bom(args)

    if args.command == "label":
        return _run_label(args)

    if args.command == "offers":
        return _run_offers(args)

    if args.command == "subs":
        return _run_subs(args)

    if args.command == "build":
        return _run_build(args)

    if args.command == "user":
        return _run_user(args)

    db.migrate()
    conn = db.connect()
    try:
        if args.key_command == "create":
            result = auth.create_key(conn, args.name, args.role)
            print(result["key"])
        elif args.key_command == "list":
            for row in auth.list_keys(conn):
                _print_key_row(row)
        elif args.key_command == "revoke":
            auth.revoke_key_by_name(conn, args.name)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
