"""Command-line interface for operators who cannot reach the UI."""

from __future__ import annotations

import argparse
import sys

from . import auth, db, enrich
from .repo import catalog
from .repo import parts as repo


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

    enrich_parser = sub.add_parser("enrich", help="run the enrichment queue")
    enrich_target = enrich_parser.add_mutually_exclusive_group(required=True)
    enrich_target.add_argument("--all", action="store_true", help="queue every part")
    enrich_target.add_argument("--ipn", metavar="IPN", help="queue a single part")
    enrich_parser.add_argument("--force", action="store_true", help="re-run parts already enriched")

    args = parser.parse_args(argv)

    if args.command == "enrich":
        return _run_enrich(args)

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
