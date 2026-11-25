"""Command-line interface for operators who cannot reach the UI."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from . import auth, db, enrich, sessions, users
from .bom import BomError
from .repo import boms as boms_repo
from .repo import catalog
from .repo import parts as repo
from .repo.boms import DuplicateRevisionError


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

    args = parser.parse_args(argv)

    if args.command == "enrich":
        return _run_enrich(args)

    if args.command == "bom":
        return _run_bom(args)

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
