# Demo runbook

A ten-minute walkthrough that runs entirely on the demo laptop. No network calls, no home
connection, nothing to log into. Everything below uses the bundle and the container image.

## What to carry

- `demo-2026-11-30.zip` (96 MB) — the database and every asset, from the real sk-sensor project
- `shelfmark-0.1.0-py3-none-any.whl` (fallback, if Docker is not available)
- Optional: three photos of supplier labels (see "Scan a label" below)

Both files live on Akasha at `/mnt/Media-Storage/Application-Storage/shelfmark-demo/`, reachable from
the workstation over SMB, or copy them with `scp`.

## Start it

```bash
docker run -d --name shelfmark-demo -p 8080:8080 \
  -v shelfmark-demo:/data \
  -v "$PWD":/bundle:ro \
  ghcr.io/stevengann/shelfmark:sha-6d72640

docker exec shelfmark-demo shelfmark restore /bundle/demo-2026-11-30.zip
```

Open <http://localhost:8080>. The instance has no accounts, so it opens on **Create the first
admin** — that is the demo's first beat, not a problem.

No Docker? The same database loads from a plain Python install:

```bash
python3 -m venv .venv && .venv/bin/pip install shelfmark-0.1.0-py3-none-any.whl
SHELFMARK_DATA_DIR=./data SHELFMARK_ASSET_DIR=./data/assets .venv/bin/shelfmark restore demo-2026-11-30.zip
SHELFMARK_DATA_DIR=./data SHELFMARK_ASSET_DIR=./data/assets .venv/bin/uvicorn app.main:app --port 8080
```

## The script

**1. Claim the instance** (30s). Create the admin account on the setup screen. One sentence: it is a
single container, one SQLite file, no cloud, nothing leaves the machine.

**2. The catalogue** (2 min). Parts. 87 parts, each with an internal part number, an MPN, a package,
a photo and a datasheet where one exists. Filter by `0402`. Open SK-00001, show the prices card, the
stock card and the links. Mention the MPN normalisation: the same part entered four ways is one part.

**3. A real board** (3 min). Projects → `sk-sensor` → `rev B`. 54 lines, 325 placements, two
revisions, a diff between them. Point at the PACKAGE column and the match column: matched lines name
their internal part, unmatched lines are the ones the catalogue has never seen. 23 of those are
deliberate — the import does not invent parts.

**4. What do I need to build ten boards?** (2 min). Plan a build → 10 boards. Then read the shortfall:
every line that is short, with how many are missing. This is the slide: it is the answer to "can we
build this, and what do we buy". Commit draws the stock atomically with the ledger; planning reserves
nothing.

**5. Where the parts are** (1 min). Stock. Two bins, the ledger per part (received, consumed, counted,
moved), and a scanned label receipt if you have a photo. Show that consuming for a build is one
transaction: a shortage aborts the whole commit rather than half-drawing it.

Then close the loop: in Stock, receive 31 of SK-00001 into Bin 12, go back to `rev B`, and watch that
line's shortage disappear. Nothing about the demo data is staged — those are the two bins that exist —
so this is the honest version of "the shortfall is a to-do list, and it updates as stock lands."


**6. It is yours** (1 min). Account → the app is one container and one file. `shelfmark export`
produced the very zip the demo started from; `restore` on a new machine brings it back. There is an
admin-only HTTP equivalent, and a nightly job keeps a fortnight on the NAS.

## Scan a label (optional, 1 min)

Stock → **Scan a supplier label** → choose a photo of a DigiKey or Mouser bag → the label is decoded
into MPN, manufacturer, quantity and lot → receive into a bin. Print two labels beforehand if there is
no bag to hand; the decoder works on a clean photo of the label, not on the bag's shape.

## Reset between rehearsals

```bash
docker rm -f shelfmark-demo && docker volume rm shelfmark-demo
```

Then repeat "Start it". The bundle is never modified.

## Do not show

The production instance, its address, the NAS layout, or the deployment pipeline. This demo is a local
copy on purpose: the laptop can be handed over, shut, and taken home with nothing exposed.

## Honest limits, if asked

No purchase orders beyond storing a PO number from a label. No serial or lot traceability past the lot
text on a bag. OpenPnP still needs its own feeder and nozzle definitions — the export gives it part
numbers and coordinates, not machine setup.
