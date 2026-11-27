# Demo runbook

A ten-minute walkthrough that runs entirely on the demo laptop. No network calls, no home
connection, nothing to log into. Everything below uses the Windows package and the demo bundle
inside it.

## What to carry

`shelfmark-windows-demo.zip` (95 MB) — the app, the database and every asset. Unzip it anywhere; it
contains:

- `start-shelfmark.bat` — one double-click, no install, no admin
- `demo.zip` — the database and assets from the real sk-sensor project
- `shelfmark-0.1.0-py3-none-any.whl` — the application itself
- `README.txt` — the same instructions, in the folder

It lives on Akasha at `/mnt/Media-Storage/Application-Storage/shelfmark-demo/`, reachable from the
workstation over SMB, or copy it with `scp`.

## First run, at home, with the internet

1. Unzip anywhere — Desktop is fine.
2. Double-click `start-shelfmark.bat`. If Windows has marked it blocked, right-click it, Properties,
   tick "Unblock", OK — a .bat that arrived from another machine carries that flag.
3. If Windows says Python is missing, run this once and double-click again:
   `winget install -e --id Python.Python.3.12`
   A `python` that opens the Microsoft Store is a stub, not an interpreter; the script checks for
   that and says so.
4. The first start builds a private environment and loads the demo data. About a minute.
5. The browser opens at <http://localhost:8080> and asks you to create the first admin. Do it — this
   is the demo's first beat, not a setup problem.

On Monday, the same double-click starts in a few seconds. Keep the console window open; closing it
stops the app. No network is used or needed while demoing.

## If something goes wrong on the day

- **Browser did not open** — go to <http://localhost:8080> yourself.
- **Port 8080 already in use** — close whatever holds it, or change `--port 8080` to `--port 8081` on
  the last line of the `.bat` and open <http://localhost:8081>.
- **Setup failed halfway** — delete the `.venv` folder and double-click again; the demo data is
  untouched.
- **Start over from clean data** — close the window and delete the `data` folder. The next start
  reloads `demo.zip`, which is never modified.

## Other machines

The same bundle runs in a container if Docker is available:

```bash
docker run -d --name shelfmark-demo -p 8080:8080 -v shelfmark-demo:/data -v "$PWD":/bundle:ro \
  ghcr.io/stevengann/shelfmark:sha-0cfcd79
docker exec shelfmark-demo shelfmark restore /bundle/demo-2026-11-30.zip
```

Or from a plain Python install on Linux or macOS:

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

Close the console window and delete the `data` folder. The next start reloads `demo.zip`, which is
never modified.

## Do not show

The production instance, its address, the NAS layout, or the deployment pipeline. This demo is a local
copy on purpose: the laptop can be handed over, shut, and taken home with nothing exposed.

## Honest limits, if asked

No purchase orders beyond storing a PO number from a label. No serial or lot traceability past the lot
text on a bag. OpenPnP still needs its own feeder and nozzle definitions — the export gives it part
numbers and coordinates, not machine setup.
