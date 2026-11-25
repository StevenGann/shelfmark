# Shelfmark

Parts inventory and BOM management for electronics assembly. Take a KiCad BOM,
know exactly what is on the shelf, and emit a pick-and-place job — without
spreadsheets, and without a database that has to be exported by hand.

Status: **M0 (skeleton)**. The API serves a health check and the schema exists;
there is no inventory UI yet.

## What it is for

Small-batch assembly has an awkward shape. The design lives in KiCad, the parts
live in a drawer, the prices live on a distributor's website, and the placement
file wants part names that match a machine library. Every tool solves one of
those and none of them solve the seam between them.

Shelfmark is built around four ideas:

- **One part, one identity.** An internal part number per real part, with as
  many supplier SKUs, manufacturer part numbers and packaging variants as it
  needs. Substitutions point at parts, not at order lines.
- **BOMs are versioned documents.** A board revision has BOM lines; lines
  reference parts, never offers. Importing a new BOM produces a diff, not a
  silent overwrite.
- **Commodity substitution is a contract, not a note.** Resistors and
  capacitors declare a parametric slot — package, value, tolerance, dielectric —
  so a shortage can be resolved by anything that fits, and the reasoning is
  recorded rather than remembered.
- **Location is free text.** Real shelves are not a taxonomy. A stock item
  carries whatever string describes where it lives, and moves are history.

## Status and roadmap

| Milestone | Contents |
| --- | --- |
| M0 | Skeleton, config, migrations, health check — **done** |
| M1 | Parts, internal part numbers, MPN normalisation |
| M2 | Supplier offers (DigiKey first) with recorded fixtures |
| M3 | BOM ingest, parameter extraction, revisions, diff |
| M4 | Commodity slots and deterministic substitution |
| M5 | Stock items, locations, movement ledger |
| M6 | Builds, shortages, cost roll-up, OpenPnP job package |
| M7 | Export/restore, nightly refresh, multi-arch image |

## Running it

Development:

```bash
uv venv
uv pip install -e ".[dev]"
uvicorn app.main:app --port 8080
curl localhost:8080/api/health
```

Container (single image, single process, no external services):

```bash
docker compose up --build
```

Tests:

```bash
pytest -q
```

## Configuration

Everything comes from the environment, prefixed `SHELFMARK_`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHELFMARK_DATA_DIR` | `./data` | Directory holding the SQLite database and job packages |
| `SHELFMARK_LISTEN_PORT` | `8080` | Listen port |
| `SHELFMARK_LOG_LEVEL` | `info` | Log verbosity |

The container runs read-only with a single writable volume at `/data`, as a
non-root user, with a health check baked into the image.

## Licence

MIT. See [LICENSE](LICENSE).
