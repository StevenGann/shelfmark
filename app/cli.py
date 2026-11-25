"""Command-line interface for operators who cannot reach the UI."""

from __future__ import annotations

import argparse

from . import auth, db


def _print_key_row(row: dict) -> None:
    revoked = row.get("revoked_at") or "-"
    last = row.get("last_used_at") or "-"
    print(
        f"{row['id']}\t{row['name']}\t{row['prefix']}\t{row['role']}\t{row['created_at']}\t{last}\t{revoked}"
    )


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

    args = parser.parse_args(argv)

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
