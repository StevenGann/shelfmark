# Shelfmark user guide

## What Shelfmark is

Shelfmark is a self-hosted parts inventory and bill-of-materials application for electronics
assembly. It closes the gap between the four tools a small-batch assembler already uses: KiCad
knows what a board needs, the drawers know what you roughly own, the distributor's website knows
price and stock, and the pick-and-place knows where the parts go. Shelfmark gives each part one
identity, turns a KiCad BOM into matched lines against your own stock, records every stock change
as a ledger entry, and hands a build to OpenPnP as a pick list and a board file. It runs as a
single container with a single SQLite database and no external services.

## Concepts

- **Part.** One real component. It has a manufacturer part number (MPN), a manufacturer, a
  package, a description, a category, a design status, and any number of supplier part numbers
  pointing at it. A part is what a BOM line references.
- **IPN (internal part number).** The identifier Shelfmark assigns to a part when you create it,
  for example `SK-00001`. The prefix comes from `SHELFMARK_IPN_PREFIX` (default `SK`); the number
  is five digits and is never reused, so a deleted part leaves a hole in the sequence.
- **Stock item.** One part at one location, with a quantity. Location is free text (`Drawer A`,
  `Shelf 3 box 2`), matched case-insensitively after collapsing spaces.
- **Stock move / ledger.** Every quantity change is a recorded row: the kind, the signed change,
  the resulting quantity, who did it and when. The quantity you see is always derived from the
  moves; you never edit it directly.
- **BOM revision.** One import of a KiCad BOM CSV against a project. Revisions are immutable; a
  corrected BOM is a new revision, and each import produces a diff against the previous one.
- **Substitute.** A directed link saying "this part may be used where that part is called for",
  with a fit level and a note. The reverse direction is a separate link.
- **Build.** "We are making N boards of this revision." A build is a plan (nothing changes) that
  becomes immutable when committed, at which point stock is consumed.

## Deploying

### The compose file

`compose.yaml` runs a single service from the Dockerfile:

```yaml
services:
  shelfmark:
    build: .
    image: shelfmark:dev
    ports:
      - "8080:8080"
    environment:
      SHELFMARK_DATA_DIR: /data
    volumes:
      - shelfmark-data:/data
    read_only: true
    tmpfs:
      - /tmp
    restart: unless-stopped

volumes:
  shelfmark-data:
```

Start it with `docker compose up --build`, then open `http://localhost:8080`. The container runs
read-only as a non-root user with one writable volume, `/data`, which holds the SQLite database
and all assets. There is one process (one Uvicorn worker) by design.

### Environment variables

Everything is read from the environment, prefixed `SHELFMARK_`. These are the real names and
defaults from `app/config.py`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHELFMARK_DATA_DIR` | `./data` | Directory holding the SQLite database (`shelfmark.db`) |
| `SHELFMARK_ASSET_DIR` | `/data/assets` | Directory for hosted datasheets, images and stored BOM files |
| `SHELFMARK_LISTEN_PORT` | `8080` | Listen port |
| `SHELFMARK_HOST` | `0.0.0.0` | Listen address |
| `SHELFMARK_LOG_LEVEL` | `info` | Log verbosity |
| `SHELFMARK_IPN_PREFIX` | `SK` | Prefix for generated internal part numbers |
| `SHELFMARK_AUTH` | `on` | `none` disables authentication; refused when `SHELFMARK_ENV=production` |
| `SHELFMARK_ENV` | `development` | Environment name |
| `SHELFMARK_RATE_LIMIT` | `600` | Requests per minute per API key; `0` disables |
| `SHELFMARK_BOOTSTRAP_KEY` | *(generated)* | The first admin API key when the key table is empty |
| `SHELFMARK_DIGIKEY_CLIENT_ID` | *(empty)* | DigiKey Product Information API v4 client id |
| `SHELFMARK_DIGIKEY_CLIENT_SECRET` | *(empty)* | DigiKey API v4 client secret |
| `SHELFMARK_DIGIKEY_DAILY_BUDGET` | `1000` | Daily cap on DigiKey product lookups |
| `SHELFMARK_QUEUE_TICK_SECONDS` | `900` | How often the app drains the enrichment queue; `0` disables |

With no DigiKey credentials, enrichment and price refresh stay queued and report "credentials not
configured"; everything else works.

### First-run bootstrap

There are two separate "first run" events.

1. **First admin API key.** On startup, after migrations, if the `api_key` table is empty the app
   creates one admin key. If `SHELFMARK_BOOTSTRAP_KEY` is set, that string is used as the key;
   otherwise a key is generated and written to `$SHELFMARK_DATA_DIR/bootstrap-key.txt` (permissions
   0600), and only its prefix is logged. Read that file (or note your supplied key) to call the API.
2. **First admin user.** When the `app_user` table is empty, the UI serves `/setup` and every
   other page redirects there. `/setup` creates the first administrator account from a username
   and password. Once one user exists, `/setup` returns 404. Because the form is claimable by
   anyone who reaches the instance first, claim a fresh deployment immediately.

### Upgrading

Migrations run automatically at startup, before any request is served. To upgrade, build or pull
the new image and restart the container; `app.db.migrate()` applies any pending `migrations/*.sql`
files in filename order, one per release, and records them in a `schema_migration` table. There is
no separate migration command to run.

## Your first hour

Walk these in order and you will have touched every part of the product. Field names below match
the forms exactly.

### 1. Log in and make yourself an admin

Open `http://localhost:8080`. On a fresh database you are sent to `/setup`; enter a username and
password and submit. Then log in at `/login`. The header shows your username and role.

### 2. Create a part

Choose **New part** (top of the list or the sticky button), or visit `/parts/new`. The form has:

- **MPN** (`mpn`) and **Manufacturer** (`manufacturer`) are required.
- **Description**, **Category**, **Notes** are optional.
- **Design status** (`design_status`) is one of `active`, `nrnd`, `obsolete`, `do-not-use`.

Submit with **Create part**. You land on the part's page, which shows its IPN. If you create the
same MPN and manufacturer twice, the form refuses and names the existing IPN.

### 3. Receive stock

On the part page, in the **Stock** panel, the top form takes a **Location** (`location`), a
**Quantity** (`quantity`) and an optional **Note** (`note`), with three buttons:

- **+ Receive** adds the quantity to that location.
- **- Consume** removes it.
- **Count** (`adjust`) records a recount: you type how many are *actually* there, and the ledger
  records the difference.

You can also receive stock for many parts at once from a supplier bag using the label scanner
(see step 6).

### 4. Import a BOM

Choose **Projects** in the header, or visit `/projects`. Create a project with a **Project name**
(`name`) and optional **Description** (`description`). Open the project, then in **Import a BOM**
choose a **Revision label** (`label`, such as `rev C`), an optional **Note** (`note`), and the
**BOM CSV** file (`file`). Submit **Preview import**.

The preview shows the diff against the previous revision: lines added, removed, changed, and
unmatched. Confirm with **Import as ...** to commit the revision. Re-importing the same file is
refused rather than creating a duplicate revision.

### 5. Read a match

Open the revision (or the project page). Each BOM line shows its references, MPN, manufacturer,
quantity, package and a **Match** column:

- A matched line shows its IPN as a link.
- An unmatched line shows an **IPN** input and a **Match** button, so you can attach the line to
  an existing part.
- A line whose MPN matches more than one part shows **ambiguous**.

The project and revision pages both offer **Create parts for unmatched**, which creates a part for
every unmatched line that names a manufacturer.

### 6. Scan a supplier label

Visit `/stock` and open the **Scan a supplier label** panel (engineer or admin). Choose the label photo
(`file`) and submit **Decode**. Shelfmark reads the Data Matrix and linear codes, shows the
decoded supplier part number, MPN, manufacturer, description, quantity, lot, date code and
country, and either matches an existing part or offers to create one. Fill the **Location** (it
defaults to where that part was last put away), adjust the **Quantity**, add a **Note**, and
submit **Confirm receipt**. Nothing is written until you confirm.

### 7. Plan and commit a build

Open a revision and use **Plan a build**: enter **Boards** (`boards`) and an optional **Note**
(`note`). The build page shows, line by line, what is required, what is on hand, what is short and
which substitutes would cover the shortfall. When you are ready, submit **Commit build**. If any
line is short the commit fails with the line, the required quantity and the shortfall, and nothing
is written.

### 8. Download the OpenPnP files

On the build page, under **Downloads**:

- **Pick list (.csv)** lists each line's IPN, MPN, manufacturer, package, value, required, on
  hand, location and substitutes.
- **OpenPnP board (.xml)** is the placement file, one placement per reference, with real
  coordinates and `part-id` set to the IPN.
- **OpenPnP job (.xml)** names the board once.

The board file requires the revision to have a position file (see "Builds").

## Roles and access

Roles are `viewer < engineer < admin` and mean the same thing for people and machines.

| Role | What it can do |
| --- | --- |
| viewer | Read everything: parts, stock, projects, BOMs, builds, downloads. No writes. |
| engineer | Everything a viewer can, plus create and edit parts, record stock, scan labels, import BOMs, resolve lines, add substitutes and offers, plan and commit builds, upload positions and assets. |
| admin | Everything an engineer can, plus manage users and API keys. |

Two credentials mechanisms:

- **Browser sessions.** People log in with username and password. The `smk_session` cookie is
  HttpOnly, SameSite=Lax, Secure on https, and expires after 30 days of inactivity (sliding). The
  password must be at least 12 characters and different from the username.
- **API keys.** Machines call the JSON API with `Authorization: Bearer <key>` or
  `X-API-Key: <key>`. Keys are `smk_` plus 40 hex characters, shown once at creation, and stored
  only as a hash. An admin creates them from the CLI (`shelfmark key create`) or `POST /api/keys`.

A session cookie does not work on `/api/*`, and an API key does not work on the HTML routes.

## The stock ledger

Quantities are never edited directly. `stock_item.quantity` is always the sum of that item's
`stock_move` deltas, and every change is one `stock_move` row naming the actor and, optionally, a
note. This is what makes a rebuild recoverable: the ledger says what happened, not just what the
count now is.

The move kinds are:

| Kind | Meaning |
| --- | --- |
| `receive` | Stock arrived. Positive. |
| `consume` | Stock was used outside a build. Negative. |
| `scrap` | Stock was discarded. Negative. |
| `adjust` | A recount. You supply the counted total; the delta is recorded. |
| `build` | Stock was consumed by committing a build. Negative. |
| `move` | Stock moved between two locations. Written as two rows, `-q` on the source and `+q` on the destination, so the part's total is unchanged. |

Moving stock between locations is double entry, so every stock item still satisfies
`quantity == SUM(delta)`.

### Correcting a mistake

Because a count is never edited, correcting a mistake means recording the correction:

- Over-received by 5: consume 5 at that location, or run a **Count** with the real number.
- Put it in the wrong bin: use the **move** form (choose the row's **+ receive / - consume / count
  / move**, then the **Move to** field) to move the quantity to the right location.
- Shelf and record disagree after a build: run **Count** with what is physically there.

There is no delete on stock rows. A move that would take a location below zero is refused with
`409` and the row is left untouched.

## BOM import

### Accepted columns

The parser maps header aliases, not positions, and matches them case-insensitively after
stripping spaces and punctuation. The full alias list from `app/bom.py`:

| Field | Accepted headers |
| --- | --- |
| references | `Reference`, `References`, `Refs`, `Ref`, `Designator`, `Designators`, `Customer Reference`, `Customer References`, `Part Reference` |
| quantity | `Qty`, `Quantity`, `Count`, `Quantity Per Board` |
| value | `Value`, `Comment` |
| footprint | `Footprint`, `Package`, `Package / Footprint`, `Case` |
| mpn | `MPN`, `BOM MPN`, `Manufacturer Part Number`, `Manufacturer PN`, `Mfr Part #`, `Mfr Part Number`, `Part Number`, `Part #`, `Supplier Part Number` |
| manufacturer | `Manufacturer`, `Mfr`, `Mfr.` |
| description | `Description`, `Desc` |
| dnp | `DNP`, `Do Not Populate`, `Exclude from BOM` |

The delimiter is sniffed among comma, semicolon and tab. A file with no recognisable MPN column is
refused, and the error lists the headers it actually saw.

### DNP handling

DNP lines are kept and marked, not dropped. A line is treated as DNP unless its DNP cell is one of
`""`, `0`, `false`, `no`, `n`, `none`. DNP lines are excluded from builds but remain part of the
design, so a diff can show them appearing or disappearing.

### How matching works

A line matches a part on `(mpn_norm, manufacturer_norm)`:

- The MPN is normalised to upper-case letters and digits only, so `RC0402FR-0710KL` and
  `rc0402fr-0710kl ` are the same part, while a difference in a digit is a different part.
- Manufacturer names are compared with a fold that ignores accents and case, so "Würth" and
  "Wurth" match. Exact spelling wins first, folding is the fallback.
- With no manufacturer in the BOM, the line matches on `mpn_norm` alone, and only when exactly one
  part carries it.

### Unmatched and ambiguous

- **Unmatched** means no part matches. The line's note says why: it names the part that exists
  under a different manufacturer, or states that no part has that MPN. Resolve it by attaching an
  IPN or by creating the part.
- **Ambiguous** means the MPN matches several parts. The note lists their IPNs. Resolve it by
  choosing the right IPN, not by bulk-create (bulk create skips ambiguous lines).

The KiCad footprint wins over a supplier's package string: when a matched part's package is empty
or differs, the part is updated to the footprint's normalised form and the change is recorded on
the line's note.

## Labels

Shelfmark reads DigiKey and Mouser bag labels from a photo, using `zxing-cpp`. It accepts JPEG,
PNG or WebP, up to 20 MB, checked by magic bytes rather than the browser's claim.

- **DigiKey** sends an ANSI MH10.8.2 Data Matrix carrying the DigiKey part number (`P` or `30P`),
  the MPN (`1P`), description (`K`), quantity (`Q`), date code (`9D`), lot (`1T`), country (`4L`)
  and purchase order (`10K`). The linear Code 128 codes on the same label are DigiKey-internal
  references and are read but never used for matching.
- **Mouser** sends the same Data Matrix shape but always includes `1V` (the manufacturer), which
  DigiKey never sends; this is how the two are told apart. Mouser's supplier part number often
  comes from the linear Code 128 (`<3-digit mfr id>-<mpn>`).

Resolution is by supplier part number first (any distributor's `part_link.sku`), then by MPN
(exactly one part or none), then unknown.

### The Mouser cases where the 2D code will not read

- Some Mouser labels omit the MPN (`1P`) from the Data Matrix entirely. The MPN is recovered from
  the linear code by stripping the three-digit manufacturer prefix, and only when the payload is
  not a DigiKey part number.
- When the 2D symbol will not read at all (a small or angled label), Shelfmark falls back to the
  linear codes: the supplier part number and MPN come from the `<mfr id>-<mpn>` payload, the
  quantity is the largest short integer, and a two-letter payload is the country. The form is
  prefilled and nothing is written until you confirm.
- A photo with no readable symbol at all returns an error listing the payloads it did read.

### Photo advice

Fill the frame with the Data Matrix, hold the phone square-on, and avoid glare on the label. Large
photos are downscaled before decoding. The scan decodes on the server, so a blurry or partial
photo simply produces an error you can retry; it never writes a wrong receipt.

### What is stored

Decoding writes nothing. Confirming a receipt stores a `receive` stock move, plus a distributor
link carrying the supplier part number (so the next scan of the same bag resolves immediately).
The move's note carries `lot <lot>, DC <date_code>` when the label has those, so traceability
survives as text. There is no separate lot or serial-number record.

## Substitutes

A substitute is a directed link: `part_id` is the part the design calls for, `substitute_part_id`
is what may be used instead. The reverse direction is a separate row. Fit levels, in decreasing
strength of claim:

| Fit | Meaning |
| --- | --- |
| `exact` | The same component under a second internal part number (different packaging or a second SKU). |
| `equivalent` | Same specifications from a different manufacturer. |
| `form-fit` | Same land pattern and function, with a stated difference. A note is required. |
| `function` | Works electrically but does not match the footprint, so it needs wire, an adapter or a re-spin. |

A part cannot substitute for itself, and a duplicate directed link is refused. The part page shows
substitutes in both directions, and adding one takes an IPN or an MPN.

### Shortfall coverage

For each BOM line, the revision and build pages compute `required`, `on_hand`, and `short` (`max(0,
required - on_hand)`). Each substitute carries its own on-hand total and a `covers` flag meaning
its stock alone would cover the shortfall. Substitution never rewrites a BOM line and never happens
automatically at commit time; it is a suggestion until you link the parts and replan.

## Builds

A build is a view over an existing BOM revision. Planning copies each non-DNP line with
`required = quantity * boards`, sets `short` against current stock, and writes nothing. Unmatched
lines are carried into the build as short, because you cannot build a board with a line nobody can
buy.

Committing consumes stock through the ledger in one transaction. It draws from a preferred
location you can name per line, otherwise from the largest stock first. If any line cannot be
covered, the commit fails with `409` naming the first short line, and nothing at all is written;
a committed build is immutable, and a planning build can be abandoned but never edited.

The OpenPnP exports are a pick list and a board file plus a job file. Positions are optional: you
upload a KiCad position file (`File > Fabrication Outputs > Component Placement` gives both a
padded `.pos` and a CSV form, and both are accepted). Without a position file a build still
commits and still produces a pick list, but the board file returns `409 positions_required`.

**Stated limitation:** the board file is a placement file only. `part-id` is the Shelfmark IPN,
but OpenPnP still needs its own part definitions for each IPN before it can place anything; the
nozzle, feeder offset and package geometry are OpenPnP's, not Shelfmark's. The pick list is what
you use to create those parts, and the board file is what stops you typing coordinates by hand. A
multi-board build names the board once; OpenPnP's board count sets the rest, because where the
other boards sit is your panel, not Shelfmark's.

## Enrichment and prices

Both exist and are optional.

**Enrichment** fetches a part's DigiKey product details (manufacturer, package, datasheet, photo,
product link and links to other distributors) with one API call per MPN. It runs from the part
page's **Fetch metadata** button, the CLI (`shelfmark enrich`), or the background queue tick
(`SHELFMARK_QUEUE_TICK_SECONDS`). It requires `SHELFMARK_DIGIKEY_CLIENT_ID` and
`SHELFMARK_DIGIKEY_CLIENT_SECRET`, and is bounded by a daily budget
(`SHELFMARK_DIGIKEY_DAILY_BUDGET`, default 1000). A part not in the DigiKey catalogue is marked
failed; missing credentials leave the job queued. An asset you upload yourself is never
overwritten by the queue.

**Prices** record offer ladders from DigiKey (via the part page's **Refresh prices** button or
`shelfmark offers refresh`) or by hand (the **Add an offer by hand** form, for distributors with
no API). Money is stored as integer micro-dollars. Given a quantity, the page and the API pick the
cheapest offer by total cost, not by the smallest unit price. `shelfmark offers audit` reports
price ladders where buying more costs less in total.

## Backup and restore

Everything worth backing up lives under one directory:

- The database at `$SHELFMARK_DATA_DIR/shelfmark.db` (in the container, `/data/shelfmark.db`).
- Assets at `$SHELFMARK_ASSET_DIR` (default `/data/assets`), holding images, datasheets and the
  stored BOM CSVs under `parts/<ipn>/...` and `boms/<project>/<sha256>.csv`.

The database runs in WAL mode, so a consistent copy must include the `-wal` and `-shm` side files,
or be taken with SQLite's backup. The simplest consistent backup is to stop the container, then
copy the whole `/data` volume:

```
docker compose stop
docker run --rm -v shelfmark-data:/data -v "$PWD":/backup alpine cp -a /data /backup/shelfmark-data
docker compose start
```

Restore is the reverse: stop the container, replace the volume contents, start again. Migrations
run automatically on start, so restoring an older database and then starting a newer image is
upgraded in place.

## Troubleshooting

### 409 shortage (build commit refused)

You committed a build whose lines cannot all be covered. The message names the line, the required
quantity, the on-hand quantity and the shortfall, for example
`SK-00021 0402 10k: need 120, have 87 — 33 short` (IPN, package, value, then the numbers). Nothing
was written. Receive or count the
missing stock, or link a substitute that covers it and replan, then commit again.

### 409 positions_required (board.xml refused)

You asked for the OpenPnP board file for a revision with no position file. Upload a KiCad
position file on the revision page, then download the board file again. The pick list and job file
do not need positions.

### 422 label errors

The label photo could not be used. The message distinguishes the cases: not a JPEG/PNG/WebP, over
20 MB, no barcode found (retry with the label filling the frame), or barcodes read but none usable
(the error lists what was read). Retake the photo and resubmit; nothing is written.

### 401 on `/api` from a browser

The JSON API only accepts API keys in the `Authorization: Bearer` or `X-API-Key` header; a browser
session cookie is not accepted there. Use a key created by an admin, or use the HTML pages instead.

### Negative stock refused (409)

A `consume`, `scrap`, `build` or move that would take a location below zero is refused with the
current quantity, and the row is untouched. If the shelf and the record disagree, use **Count**
(`adjust`) with the real number rather than consuming more than you have.

### Duplicate part / duplicate IPN (409)

Creating a part whose normalised MPN and manufacturer already exist is refused, and the error
names the existing IPN. Use that part, or correct the MPN or manufacturer. The IPN itself is
assigned once and cannot be changed.
