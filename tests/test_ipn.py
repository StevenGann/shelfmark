import pytest

from app import db
from app.ipn import format_ipn
from app.repo import parts as repo
from app.repo.parts import DuplicatePartError


def test_format():
    assert format_ipn(1) == "SK-00001"
    assert format_ipn(25) == "SK-00025"
    assert format_ipn(99999) == "SK-99999"


def test_format_prefix_override():
    assert format_ipn(1, prefix="XX") == "XX-00001"


def test_allocation_is_monotonic():
    conn = db.connect()
    try:
        ipns = []
        for i in range(25):
            part = repo.create_part(conn, mpn=f"MPN-{i:04d}", manufacturer="Acme")
            ipns.append(part["ipn"])
        assert ipns == [f"SK-{n:05d}" for n in range(1, 26)]
        assert len(set(ipns)) == 25
    finally:
        conn.close()


def test_ipn_is_immutable_under_update():
    conn = db.connect()
    try:
        part = repo.create_part(conn, mpn="LM358N", manufacturer="TI")
        original = part["ipn"]
        updated = repo.update_part(conn, original, mpn="LM358P", notes="revised")
        assert updated is not None
        assert updated["ipn"] == original
        assert updated["mpn"] == "LM358P"
    finally:
        conn.close()


def test_duplicate_create_does_not_consume_a_number():
    conn = db.connect()
    try:
        first = repo.create_part(conn, mpn="LM358N", manufacturer="TI")
        assert first["ipn"] == "SK-00001"
        with pytest.raises(DuplicatePartError) as exc:
            repo.create_part(conn, mpn="lm358n", manufacturer="TI")
        assert exc.value.ipn == "SK-00001"
        second = repo.create_part(conn, mpn="NE555", manufacturer="TI")
        assert second["ipn"] == "SK-00002"
    finally:
        conn.close()
