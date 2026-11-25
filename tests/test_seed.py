import io

from app import db
from app.seed import categorise, rows_from, seed


KICAD = (
    "Reference,Qty,Value,DNP,Manufacturer_Name,Manufacturer_Part_Number,Description\n"
    '\"C201,C202\",2,2.2uF,,Murata Electronics,GRM155Z71A225KE44D,'
    '"MLCC - SMD/SMT 2.2 uF 10 VDC 0402 X7R"\n'
    'R1,1,4k7,,Yageo,RC0402JR-7D4K7L,"RES 4.7K OHM 0402"\n'
    ",,,,,,\n"
)

DIGIKEY = (
    "Digi-Key Part Number,Customer Reference,Quantity,Manufacturer Part Number,"
    "Manufacturer,Description\n"
    "296-53025-1-ND,,12,DAC81416RHAT,Texas Instruments,IC DAC 16BIT 40-VQFN\n"
)


def test_rows_from_kicad_skips_blank_rows():
    rows = rows_from(io.StringIO(KICAD), "kicad")
    assert len(rows) == 2
    assert rows[0]["mpn"] == "GRM155Z71A225KE44D"
    assert rows[0]["category"] == "capacitor"
    assert rows[1]["category"] == "resistor"
    assert "value 2.2uF" in rows[0]["notes"]


def test_rows_from_digikey_keeps_the_supplier_number_as_a_note():
    rows = rows_from(io.StringIO(DIGIKEY), "digikey")
    assert rows == [
        {
            "mpn": "DAC81416RHAT",
            "manufacturer": "Texas Instruments",
            "description": "IC DAC 16BIT 40-VQFN",
            "category": "integrated-circuit",
            "notes": "296-53025-1-ND",
        }
    ]


def test_categorise_returns_none_for_unknown_text():
    assert categorise("") is None
    assert categorise("something else entirely") is None


def test_seed_is_idempotent():
    db.migrate()
    rows = rows_from(io.StringIO(KICAD), "kicad")
    first = seed(rows)
    assert first["created"] == 2 and first["skipped"] == 0

    second = seed(rows)
    assert second["created"] == 0 and second["skipped"] == 2

    conn = db.connect()
    try:
        n = conn.execute("SELECT count(*) AS n FROM part").fetchone()["n"]
    finally:
        conn.close()
    assert n == 2
