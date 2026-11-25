# Shelfmark API reference

This document lists every HTTP route in Shelfmark, grouped by resource, with its authentication
requirement, request shape, response shape and the error codes it can return. It is generated from
the route definitions in `app/*_api.py`, `app/web.py` and `app/main.py`.

## Authentication and roles

Two mechanisms, one role ladder (`viewer < engineer < admin`):

- **API keys** (`smk_` plus 40 hex characters) authenticate the JSON API under `/api/*`. Send one
  in the `Authorization: Bearer <key>` header or the `X-API-Key: <key>` header.
- **Session cookies** (`smk_session`) authenticate the server-rendered HTML routes described at
  the end of this document. A session cookie is **not** accepted on `/api/*`, and an API key is
  not accepted on the HTML routes.

Role requirements use the minimum role: a route marked "viewer" accepts viewer, engineer and
admin; "engineer" accepts engineer and admin; "admin" accepts only admin.

Common error codes:

| Code | Meaning |
| --- | --- |
| 401 | Missing, unknown or revoked API key (JSON body `{"detail": "missing or invalid API key"}`) |
| 403 | The key's role is too low for the route |
| 404 | The named resource does not exist |
| 409 | A conflict: duplicate, shortage, or a state that forbids the action |
| 422 | A request body failed validation or parsing |
| 429 | Rate limit exceeded; includes a `Retry-After` header |
| 502 | A DigiKey upstream call failed during an offer refresh |

`GET /api/health` is the only unauthenticated API route.

## Health

### `GET /api/health`

No authentication. Returns whether the process is up and whether the database answers and has
been migrated.

Response:

```json
{
  "status": "ok",
  "db": "ok",
  "version": "0.1.0",
  "data_dir": "/data"
}
```

## Parts

Router prefix `/api/parts`. List and read require `viewer`; create, update and enrich require
`engineer`.

A part object (the shape returned by every part route) is illustrative:

```json
{
  "id": 42,
  "ipn": "SK-00042",
  "mpn": "RC0402FR-0710KL",
  "mpn_norm": "RC0402FR0710KL",
  "manufacturer_id": 7,
  "manufacturer": "Yageo",
  "manufacturer_website": null,
  "package": "0402",
  "description": "RES 10K OHM 1% 1/16W 0402",
  "category": "resistor",
  "parameters_json": "{}",
  "commodity_class_id": null,
  "design_status": "active",
  "notes": null,
  "created_at": "2026-11-25T06:00:00Z",
  "updated_at": "2026-11-25T06:00:00Z"
}
```

### `GET /api/parts`

Query parameters: `q` (string, default empty), `category` (string, optional), `limit` (integer,
1 to 200, default 50), `offset` (integer, >= 0, default 0). Search `q` matches IPN, MPN,
normalised MPN, manufacturer, description and package. `package` and `sort` are UI-only and are
not accepted here.

Response:

```json
{
  "items": [ { "part": "object", "on_hand": 120 } ],
  "total": 42,
  "limit": 50,
  "offset": 0
}
```

Each item is the part object plus `on_hand` (total stock across locations).

### `POST /api/parts`

Engineer. JSON body:

```json
{
  "mpn": "RC0402FR-0710KL",
  "manufacturer": "Yageo",
  "description": "RES 10K OHM 1% 1/16W 0402",
  "category": "resistor",
  "notes": null,
  "design_status": "active"
}
```

`mpn` and `manufacturer` are required. `design_status` is one of `active`, `nrnd`, `obsolete`,
`do-not-use` (default `active`). Returns `201` with the created part. Errors: `409`
`{"ipn": "SK-00042"}` on a duplicate MPN and manufacturer; `422` on an invalid body.

### `GET /api/parts/{ipn}`

Returns the part, or `404`.

### `POST /api/parts/{ipn}/enrich`

Engineer. Queues a DigiKey enrichment job for the part and returns `202`:

```json
{ "job_id": 12 }
```

Errors: `404`.

### `PATCH /api/parts/{ipn}`

Engineer. Partial update. Accepted fields are `mpn`, `manufacturer`, `description`, `category`,
`notes`, `design_status`, `package`, `value`. The IPN itself is immutable. `mpn`, `manufacturer`
and `design_status`, when present, may not be `null`. Returns the updated part. Errors: `404`,
`409` `{"ipn": "..."}` on a duplicate, `422` on an unknown or invalid field.

## Stock

No shared prefix. Reads require `viewer`; writes require `engineer`.

### `GET /api/parts/{ipn}/stock`

Response:

```json
{
  "total": 47,
  "items": [
    { "location": "Drawer A", "quantity": 12, "updated_at": "2026-11-25T06:00:00Z" },
    { "location": "Shelf 3 box 2", "quantity": 35, "updated_at": "2026-11-25T06:00:00Z" }
  ]
}
```

Errors: `404`.

### `POST /api/parts/{ipn}/stock/moves`

Engineer. JSON body:

```json
{ "location": "Drawer A", "kind": "receive", "quantity": 25, "note": "DigiKey order" }
```

`kind` is one of `receive`, `consume`, `adjust`, `scrap`, `build`. For `receive` the quantity is a
positive count to add; for `consume`/`scrap`/`build` a positive count to remove; for `adjust` it
is the counted total, not a delta. `quantity` must be >= 0. Returns `201` with the updated stock
item (id, part_id, location, location_norm, quantity, created_at, updated_at). Errors: `404`,
`409` when the move would take a location below zero, `422` on a bad quantity or kind.

### `GET /api/stock`

Query parameters: `location` (optional), `q` (optional), `below` (integer >= 0, optional). Returns
`{"items": [...]}` where each item is a stock row with `stock_item_id`, `part_id`, `location`,
`location_norm`, `quantity`, `updated_at`, `ipn`, `mpn`, `description` and `manufacturer`.

### `GET /api/locations`

Returns distinct locations with the number of parts stocked at each:

```json
{
  "items": [
    { "location": "Drawer A", "count": 3 },
    { "location": "Shelf 3 box 2", "count": 1 }
  ]
}
```

### `POST /api/stock/move`

Engineer. JSON body:

```json
{ "ipn": "SK-00042", "from_location": "Drawer A", "to_location": "Bin 12", "quantity": 5 }
```

Moves a quantity between two locations of one part. Returns:

```json
{ "from": { "stock": "item" }, "to": { "stock": "item" } }
```

Errors: `404` (unknown part), `409` (no stock at source, insufficient quantity, or source equals
destination), `422`.

## Projects and BOM revisions

No shared prefix. Reads require `viewer`; writes require `engineer`.

### `GET /api/projects`

Returns `{"items": [...]}`. Each project carries `id`, `name`, `name_norm`, `description`,
`created_at`, plus `revision_count`, `newest_label` and `unmatched_count` (the unmatched count of
the newest revision).

### `POST /api/projects`

Engineer. JSON body `{"name": "sk-sensor", "description": null}`. Returns `201` with the project.
Errors: `409` `{"project_id": 1}` on a duplicate name, `422` on an empty name.

### `GET /api/projects/{project_id}`

Response:

```json
{
  "project": { "project": "object" },
  "revisions": [ { "revision": "object" } ],
  "diff": { "diff": "object" }
}
```

`diff` is the diff of the newest revision against the one before it (or `null` with no previous).
Errors: `404`.

### `POST /api/projects/{project_id}/boms`

Engineer. `multipart/form-data` with `file` (the CSV), `label` (required), `note` (optional), and
`dry_run` (optional; set `1`/`true`/`yes`/`on` in the form or query string to parse without
writing). Returns `201`. The response is the diff shape:

```json
{
  "dry_run": false,
  "revision_id": 5,
  "revision": { "label": "rev C", "line_count": 52, "imported_at": "2026-11-25T06:00:00Z" },
  "previous": { "label": "rev B", "imported_at": "2026-11-24T06:00:00Z" },
  "added": [ { "mpn": "RC0402FR-0710KL", "quantity": 3, "references": ["R1", "R2", "R3"] } ],
  "removed": [],
  "changed": [
    {
      "mpn": "GRM155R71C104KA88D",
      "from": { "quantity": 3 },
      "to": { "quantity": 4 },
      "references_added": ["R9"],
      "references_removed": []
    }
  ],
  "unchanged_count": 45,
  "unmatched": [ { "mpn": "X-NO-SUCH-PART", "manufacturer": null, "quantity": 1 } ]
}
```

On a `dry_run` the response has `"dry_run": true` and `"revision_id": null`. Errors: `404`
(unknown project), `422` (`file is required`, `label is required`, a BOM parse error, or a
no-MPN-column error `{"error": "no MPN column", "headers": [...]}`), `409`
`{"revision_id": 5}` when a byte-identical file was already imported.

### `GET /api/revisions/{revision_id}`

Response `{"revision": {...}, "lines": [...]}`. Each line carries `id`, `revision_id`, `part_id`,
`ipn` (when matched), `mpn`, `mpn_norm`, `manufacturer`, `manufacturer_norm`, `description`,
`value`, `footprint`, `package`, `quantity`, `references`, `dnp`, `match_status`, `match_note`,
`created_at`. Errors: `404`.

### `GET /api/revisions/{revision_id}/diff`

Returns the diff against the previous revision of the same project (same shape as the `diff` key
above). Errors: `404`.

### `POST /api/bom-lines/{line_id}/resolve`

Engineer. JSON body `{"ipn": "SK-00042"}`. Attaches an existing part to a BOM line. Returns the
updated line. Errors: `404` (no such line, or no part with that IPN).

### `POST /api/revisions/{revision_id}/resolve-unmatched`

Engineer. Creates a part for every `unmatched` line that carries a manufacturer, and attaches
them. Returns `{"created": [ { "part": "object" } ]}`. Lines with no manufacturer (or an
ambiguous match) are left alone. Errors: `404`.

## Builds and positions

No shared prefix. Reads require `viewer`; writes require `engineer`.

A build object carries `id`, `revision_id`, `boards`, `status` (`planning` | `committed` |
`abandoned`), `note`, `created_by`, `created_at`, `committed_at`, `revision_label`, `project_id`,
`project_name`, and a `lines` array. Each line carries `id`, `build_id`, `bom_line_id`, `part_id`,
`ipn`, `mpn`, `manufacturer`, `package`, `value`, `required`, `consumed`, `short`, `on_hand`,
`references`, `dnp`, `match_status`, and `substitutes` (a list of substitute links each with
`substitute_ipn`, `fit`, `substitute_on_hand` and `covers`).

### `POST /api/projects/{project_id}/boms/{revision_id}/builds`

Engineer. JSON body `{"boards": 10, "note": "first run"}`. Plans a build against the revision.
`boards` must be >= 1. Returns `201` with the build. Errors: `404` (revision not in that
project), `422` (`boards` too small).

### `GET /api/builds`

Returns `{"items": [ { "build": "object" } ]}`.

### `GET /api/builds/{build_id}`

Returns the build. Errors: `404`.

### `POST /api/builds/{build_id}/commit`

Engineer. Optional JSON body `{"locations": {"<build_line_id>": "Bin 12"}}`, which names a
preferred location per build line. Consumes stock for every line in one transaction. Returns the
committed build. Errors: `404` (no such build), `409` when the build is not `planning`, or `409`
when a line cannot be covered. The shortage `409` body is illustrative:

```json
{ "detail": "SK-00021 0402 10k: need 120, have 87 - 33 short" }
```

### `POST /api/builds/{build_id}/abandon`

Engineer. Marks a `planning` build `abandoned`. Returns the build. Errors: `404`, `409` when the
build is not `planning`.

### `POST /api/revisions/{revision_id}/positions`

Engineer. `multipart/form-data` with `file` (a KiCad `.pos` or CSV position file). Replaces the
revision's placements. Returns:

```json
{ "sha256": "0f3c...", "count": 142 }
```

Errors: `404`, `422` (`file is required` or a position parse error naming the bad line).

### `GET /api/builds/{build_id}/picklist.csv`

Viewer. Returns `text/csv`. Errors: `404`.

### `GET /api/builds/{build_id}/board.xml`

Viewer. Returns `application/xml` (an OpenPnP board file). Includes an `X-Skipped-Placements`
header with the number of placements that had no matching line. Errors: `404`, `409`
`{"detail": "positions_required"}` when the revision has no positions.

### `GET /api/builds/{build_id}/job.xml`

Viewer. Returns `application/xml` (an OpenPnP job file naming the board once). Errors: `404`.

## API keys

Router prefix `/api/keys`. Admin only.

### `GET /api/keys`

Returns `{"items": [...]}`. Each key has `id`, `name`, `prefix`, `role`, `created_at`,
`last_used_at`, `revoked_at`. The plaintext and the hash are never returned.

### `POST /api/keys`

JSON body `{"name": "ci", "role": "engineer"}`. Returns `201` with the plaintext exactly once:

```json
{ "id": 1, "key": "smk_0a1b2c...", "name": "ci", "role": "engineer" }
```

Errors: `422` (invalid role), `409` (`name already exists`).

### `POST /api/keys/{key_id}/revoke`

Returns `204`. Revoking is idempotent; a missing id still returns `204`.

## Labels

No shared prefix. Both endpoints require `engineer` and accept `multipart/form-data`.

### `POST /api/labels/decode`

Form field `file` (a JPEG, PNG or WebP image of a DigiKey or Mouser label, up to 20 MB). Decodes
and resolves, writing nothing. Response is illustrative:

```json
{
  "payloads": [ "[)>...", "660-RN73H1ETP1001F25" ],
  "label": {
    "distributor_pn": "660-RN73H1ETP1001F25",
    "mpn": "RN73H1ETP1001F25",
    "manufacturer": "KOA Speer",
    "description": null,
    "quantity": 20,
    "lot": "5397196ML5",
    "date_code": "2524+5",
    "country": "DE",
    "purchase_order": null,
    "supplier": "Mouser",
    "fields": {},
    "raw": "[)>..."
  },
  "resolution": {
    "status": "matched",
    "matched_by": "mpn",
    "part": { "part": "object" },
    "suggested_location": "Drawer A",
    "stock": [ { "stock": "item" } ]
  }
}
```

Errors: `422` with `{"message": "...", "payloads": [...]}` on an unreadable image, an unsupported
type, an oversized file, or a photo with no usable symbol.

### `POST /api/labels/receive`

Form fields: `file`, `location` (string), and optional `quantity` (integer), `note`,
`create_part` (`1`/`true`/`yes`/`on`) and `manufacturer`. The label is re-decoded server-side; the
client's decoded fields are never trusted. If the label matches no part and `create_part` is set,
a part is created from the label, using `manufacturer` (required in that case). Records a
`receive` stock move whose note carries the lot and date code. Returns `201`:

```json
{
  "part": { "part": "object" },
  "move": { "stock": "item" },
  "label": { "label": "object" }
}
```

Errors: `422` (file or manufacturer missing, bad quantity, or a stock error), `409` when creating
a part hits a duplicate (`{"message": "...", "ipn": "SK-..."}`).

## Offers and prices

No shared prefix. Reads require `viewer`; writes require `engineer`. Money is integer
micro-dollars throughout.

### `GET /api/parts/{ipn}/offers`

Query parameter `quantity` (integer >= 1, default 1). Returns:

```json
{
  "quantity": 25,
  "offers": [
    {
      "offer": {
        "id": 3,
        "part_id": 42,
        "distributor_id": 1,
        "distributor": "DigiKey",
        "sku": "311-10.0KFRCT-ND",
        "packaging": "Cut Tape (CT)",
        "url": null,
        "currency": "USD",
        "minimum_quantity": 1,
        "stock_quantity": null,
        "source": "digikey",
        "fetched_at": "2026-11-25T06:00:00Z",
        "created_by": null,
        "breaks": [
          { "quantity": 1, "unit_price_micros": 23660 },
          { "quantity": 100, "unit_price_micros": 11000 }
        ]
      },
      "unit_price_micros": 23660,
      "total_micros": 591500,
      "eligible": true
    }
  ],
  "best": 3
}
```

`offers` is sorted cheapest-total-first; `best` is the id of the winning offer (or `null`).
Errors: `404`.

### `POST /api/parts/{ipn}/offers`

Engineer. JSON body:

```json
{
  "distributor": "DigiKey",
  "sku": "311-10.0KFRCT-ND",
  "packaging": "Cut Tape (CT)",
  "url": null,
  "currency": "USD",
  "minimum_quantity": 1,
  "stock_quantity": null,
  "breaks": [
    { "quantity": 1, "unit_price_micros": 23660 },
    { "quantity": 100, "unit_price_micros": 11000 }
  ]
}
```

`distributor` must match a seeded distributor by name. Returns `201` with the offer. Errors:
`404`, `422` (`unknown distributor`, or an invalid break), `409` when an offer with that
distributor and SKU already exists.

### `DELETE /api/offers/{offer_id}`

Engineer. Returns `{"deleted": 3}`. Errors: `404`.

### `POST /api/parts/{ipn}/offers/refresh`

Engineer. Fetches the part's DigiKey ladders and replaces them. Returns:

```json
{ "refreshed": true, "offers": [ { "offer": "object" } ] }
```

Errors: `404`, `502` when a DigiKey call fails (missing credentials, exhausted budget, catalogue
miss, or a transient upstream error).

## Substitutions

Router prefix `/api/parts`. Reads require `viewer`; writes require `engineer`.

### `GET /api/parts/{ipn}/substitutes`

Query parameter `direction` (`out` default, or `in`). Returns:

```json
{
  "items": [
    {
      "id": 1,
      "part_id": 42,
      "part_ipn": "SK-00042",
      "part_mpn": "RC0402FR-0710KL",
      "part_package": "0402",
      "part_on_hand": 120,
      "substitute_part_id": 51,
      "substitute_ipn": "SK-00051",
      "substitute_mpn": "AC0402FR-0710KL",
      "substitute_package": "0402",
      "substitute_on_hand": 200,
      "fit": "equivalent",
      "note": "same 2.2uF 10V X7R",
      "created_by": "sydney",
      "created_at": "2026-11-25T06:00:00Z"
    }
  ],
  "direction": "out"
}
```

`direction=in` lists who may stand in for this part. Errors: `404`.

### `POST /api/parts/{ipn}/substitutes`

Engineer. JSON body `{"substitute": "SK-00051", "fit": "equivalent", "note": "..."}`.
`substitute` may be an IPN or an MPN. `fit` is one of `exact`, `equivalent`, `form-fit`,
`function`; `form-fit` requires a non-empty `note`. Returns `201` with the link. Errors: `404`
(unknown substitute part), `422` (self-link, invalid fit, or missing note for `form-fit`), `409`
(link already exists).

### `DELETE /api/parts/{ipn}/substitutes/{substitute_ipn}`

Engineer. Returns `{"deleted": true}`. Errors: `404` (unknown part or no such link).

### `GET /api/parts/{ipn}/substitute-suggestions`

Returns `{"items": [...]}`: parts not yet linked that share this part's `package` and `value_norm`.
Errors: `404`.

## HTML routes

These are server-rendered pages (Jinja2), authenticated by the `smk_session` cookie. They are
listed for completeness; the request and response are HTML, not JSON. Unless noted, they require a
session and redirect to `/login` when there is none.

| Method | Path | Minimum role | Purpose |
| --- | --- | --- | --- |
| GET | `/setup` | none (only when no users exist) | create the first admin |
| POST | `/setup` | none (only when no users exist) | create the first admin |
| GET | `/login` | none | login form |
| POST | `/login` | none | login, set session cookie |
| POST | `/logout` | none | end session |
| GET | `/account` | viewer | change own password |
| POST | `/account` | viewer | change own password |
| GET | `/users` | admin | list users |
| POST | `/users` | admin | create user |
| POST | `/users/{id}/role` | admin | change role |
| POST | `/users/{id}/disable` | admin | disable user |
| POST | `/users/{id}/enable` | admin | re-enable user |
| POST | `/users/{id}/password` | admin | reset a password |
| GET | `/` | viewer | parts list |
| GET | `/parts/new` | viewer | new part form |
| POST | `/parts/new` | engineer | create part |
| GET | `/parts/{ipn}` | viewer | part detail |
| POST | `/parts/{ipn}` | engineer | update part |
| POST | `/parts/{ipn}/offers` | engineer | add a manual offer |
| POST | `/parts/{ipn}/offers/refresh` | engineer | refresh DigiKey offers |
| POST | `/offers/{offer_id}/delete` | engineer | delete a manual offer |
| POST | `/parts/{ipn}/enrich` | engineer | queue enrichment |
| POST | `/parts/{ipn}/stock` | engineer | record a stock move |
| POST | `/parts/{ipn}/stock/move` | engineer | move stock between locations |
| POST | `/parts/{ipn}/substitutes` | engineer | add a substitute |
| POST | `/parts/{ipn}/substitutes/delete` | engineer | remove a substitute |
| POST | `/parts/{ipn}/assets/{kind}` | engineer | upload an image or datasheet |
| DELETE | `/parts/{ipn}/assets/{kind}` | engineer | remove an upload |
| GET | `/assets/{rel_path}` | viewer | serve an image or datasheet |
| GET | `/stock` | viewer | stock across all parts |
| POST | `/stock/scan` | engineer | decode a label photo |
| POST | `/stock/label-receive` | engineer | confirm a scanned receipt |
| GET | `/projects` | viewer | project list |
| POST | `/projects` | engineer | create a project |
| GET | `/projects/{id}` | viewer | project and revisions |
| POST | `/projects/{id}/boms` | engineer | preview or confirm a BOM import |
| GET | `/revisions/{id}` | viewer | revision lines |
| POST | `/bom-lines/{id}/resolve` | engineer | attach a part to a line |
| POST | `/revisions/{id}/resolve-unmatched` | engineer | create parts for unmatched lines |
| GET | `/builds` | viewer | build list |
| GET | `/builds/{id}` | viewer | build detail |
| POST | `/revisions/{id}/builds` | engineer | plan a build |
| POST | `/revisions/{id}/positions` | engineer | upload a position file |
| POST | `/builds/{id}/commit` | engineer | commit a build |
| POST | `/builds/{id}/abandon` | engineer | abandon a build |
| GET | `/builds/{id}/picklist.csv` | viewer | download the pick list |
| GET | `/builds/{id}/board.xml` | viewer | download the OpenPnP board file |
| GET | `/builds/{id}/job.xml` | viewer | download the OpenPnP job file |

HTML error behaviour worth knowing: an unauthenticated browser request to an HTML route redirects
to `/login` (or `/setup` when no users exist) rather than returning JSON; a viewer hitting an
engineer form gets a `403`; form validation errors re-render the page with the message and the
entered values preserved. The `409`s listed in the troubleshooting section of the user guide
apply here too: a short build commit, a `positions_required` board download, a duplicate
revision import, and a duplicate part.
