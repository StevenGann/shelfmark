import sqlite3
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

_PRE_0005 = ("0001_init.sql", "0002_parts.sql", "0003_api_keys.sql", "0004_users.sql")


def _fresh_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("PRAGMA foreign_keys = ON")
    for name in _PRE_0005:
        conn.executescript((MIGRATIONS / name).read_text())
    return conn


def test_migration_backfills_manufacturers_and_keeps_every_ipn(tmp_path):
    conn = _fresh_db(tmp_path)
    conn.execute(
        "INSERT INTO part (ipn, mpn, mpn_norm, manufacturer, created_at, updated_at) "
        "VALUES ('SK-00001', 'LM358N', 'LM358N', 'TI', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO part (ipn, mpn, mpn_norm, manufacturer, created_at, updated_at) "
        "VALUES ('SK-00002', 'GRM155R71C104KA88D', 'GRM155R71C104KA88D', "
        "'Murata Electronics', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO part (ipn, mpn, mpn_norm, manufacturer, created_at, updated_at) "
        "VALUES ('SK-00003', 'NE555', 'NE555', 'TI', 'now', 'now')"
    )
    conn.commit()

    conn.executescript((MIGRATIONS / "0005_part_assets.sql").read_text())
    conn.commit()

    ipns = {row[0] for row in conn.execute("SELECT ipn FROM part")}
    assert ipns == {"SK-00001", "SK-00002", "SK-00003"}

    manufacturers = {row[0] for row in conn.execute("SELECT name FROM manufacturer")}
    assert manufacturers == {"TI", "Murata Electronics"}

    row = conn.execute(
        "SELECT m.name FROM part p JOIN manufacturer m ON m.id = p.manufacturer_id "
        "WHERE p.ipn = 'SK-00003'"
    ).fetchone()
    assert row[0] == "TI"

    distributors = {row[0] for row in conn.execute("SELECT name FROM distributor")}
    assert distributors == {"DigiKey", "Mouser", "LCSC", "TME", "Farnell", "Octopart"}
    conn.close()
