"""The break-glass path: account management without the web UI."""

from __future__ import annotations

import io
import sys

from app import cli, db, users


def _run(monkeypatch, argv, stdin=""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return cli.main(argv)


def test_create_then_list(monkeypatch, capsys):
    code = _run(monkeypatch, ["user", "create", "alice", "--role", "engineer"], "correct-horse-9\n")
    assert code == 0

    code = _run(monkeypatch, ["user", "list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "engineer" in out


def test_short_password_is_refused(monkeypatch, capsys):
    code = _run(monkeypatch, ["user", "create", "bob", "--role", "viewer"], "short\n")
    assert code == 1
    assert "12" in capsys.readouterr().err


def test_passwd_changes_the_password_and_ends_sessions(monkeypatch, capsys):
    _run(monkeypatch, ["user", "create", "carol", "--role", "admin"], "original-pass-1\n")
    conn = db.connect()
    user = users.get_user_by_username(conn, "carol")
    from app import sessions

    sessions.start(conn, int(user["id"]))
    conn.close()

    code = _run(monkeypatch, ["user", "passwd", "carol"], "replacement-pass-2\n")
    assert code == 0

    conn = db.connect()
    fresh = users.get_user_by_username(conn, "carol")
    sessions_left = conn.execute(
        "select count(*) from user_session where user_id = ?", (int(user["id"]),)
    ).fetchone()[0]
    conn.close()

    assert users.verify_password(fresh["password_hash"], "replacement-pass-2")
    assert not users.verify_password(fresh["password_hash"], "original-pass-1")
    assert sessions_left == 0


def test_last_admin_cannot_be_disabled_or_demoted(monkeypatch, capsys):
    _run(monkeypatch, ["user", "create", "root-one", "--role", "admin"], "root-one-pass-1\n")

    code = _run(monkeypatch, ["user", "disable", "root-one"])
    assert code == 1
    assert "last active admin" in capsys.readouterr().err

    code = _run(monkeypatch, ["user", "role", "root-one", "viewer"])
    assert code == 1
    assert "last active admin" in capsys.readouterr().err

    conn = db.connect()
    assert users.get_user_by_username(conn, "root-one")["role"] == "admin"
    conn.close()


def test_demoting_one_of_two_admins_is_allowed(monkeypatch, capsys):
    _run(monkeypatch, ["user", "create", "admin-a", "--role", "admin"], "admin-a-pass-11\n")
    _run(monkeypatch, ["user", "create", "admin-b", "--role", "admin"], "admin-b-pass-11\n")

    code = _run(monkeypatch, ["user", "role", "admin-b", "engineer"])
    assert code == 0

    conn = db.connect()
    assert users.get_user_by_username(conn, "admin-b")["role"] == "engineer"
    conn.close()
