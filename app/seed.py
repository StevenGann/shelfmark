"""Bulk-load parts from a BOM export.

Used to populate a test instance from real design data. It writes through the
same repository layer as the API, so the MPN normalisation and IPN allocation
rules apply exactly as they do for a part created by hand.

    python -m app.seed --format kicad  < symkin_sensor_BOM_final.csv
    python -m app.seed --format digikey < digikey_order.csv

Idempotent: a part whose normalised MPN and manufacturer already exist is
skipped, not duplicated.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from typing import TextIO

from . import db
from .mpn import normalise_mpn
from .repo import parts as repo
from .repo.parts import DuplicatePartError

# Description-driven categorisation. Crude on purpose: it only has to be better
# than an empty column for a test data set, and the real category comes from the
# commodity contract at ingest time.
_CATEGORY_HINTS = (
    ("MLCC", "capacitor"),
    ("CAPACITOR", "capacitor"),
    ("RESISTOR", "resistor"),
    ("RES ", "resistor"),
    ("FERRITE", "ferrite"),
    ("INDUCTOR", "inductor"),
    ("LED", "led"),
    ("CONNECTOR", "connector"),
    ("HEADER", "connector"),
    ("IC ", "integrated-circuit"),
    ("OP AMP", "integrated-circuit"),
    ("DAC", "integrated-circuit"),
    ("ADC", "integrated-circuit"),
    ("CRYSTAL", "oscillator"),
    ("OSCILLATOR", "oscillator"),
)


def categorise(description: str) -> str | None:
    upper = description.upper()
    for needle, category in _CATEGORY_HINTS:
        if needle in upper:
            return category
    return None


def rows_from(stream: TextIO, fmt: str) -> list[dict[str, str]]:
    reader = csv.DictReader(stream)
    out: list[dict[str, str]] = []
    for raw in reader:
        if fmt == "kicad":
            mpn = (raw.get("Manufacturer_Part_Number") or "").strip()
            manufacturer = (raw.get("Manufacturer_Name") or "").strip()
            description = (raw.get("Description") or "").strip()
            value = (raw.get("Value") or "").strip()
            dnp = (raw.get("DNP") or "").strip()
            note = f"value {value}" if value else ""
            if dnp:
                note = f"{note}; DNP {dnp}".strip("; ")
        elif fmt == "digikey":
            mpn = (raw.get("Manufacturer Part Number") or "").strip()
            manufacturer = (raw.get("Manufacturer") or "").strip()
            description = (raw.get("Description") or "").strip()
            note = (raw.get("Digi-Key Part Number") or "").strip()
        else:
            raise SystemExit(f"unknown format: {fmt}")
        if not mpn or not manufacturer:
            continue
        out.append(
            {
                "mpn": mpn,
                "manufacturer": manufacturer,
                "description": description or None,
                "category": categorise(description),
                "notes": note or None,
            }
        )
    return out


def seed(rows: list[dict[str, str]]) -> dict[str, int]:
    created = skipped = failed = 0
    conn = db.connect()
    try:
        for row in rows:
            try:
                normalise_mpn(row["mpn"])
            except ValueError:
                failed += 1
                continue
            try:
                repo.create_part(conn, **row)
                created += 1
            except DuplicatePartError:
                skipped += 1
            except ValueError:
                failed += 1
    finally:
        conn.close()
    return {"created": created, "skipped": skipped, "failed": failed, "rows": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.seed", description=__doc__)
    parser.add_argument("--format", required=True, choices=("kicad", "digikey"))
    parser.add_argument("--csv", default="-", help="path to the CSV, or - for stdin")
    args = parser.parse_args(argv)

    db.migrate()
    stream = sys.stdin if args.csv == "-" else open(args.csv, newline="")
    try:
        rows = rows_from(stream, args.format)
    finally:
        if stream is not sys.stdin:
            stream.close()

    result = seed(rows)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
