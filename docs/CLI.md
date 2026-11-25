# Shelfmark CLI reference

The container ships a console script, `shelfmark` (entry point `app.cli:main`). It is the
break-glass path for the cases the web UI cannot reach: creating an account when nobody can log
in, revoking a key, importing a BOM from a machine that already has the CSV, and receiving stock
from a label photo without a browser.

Every command runs the migration step first, so running any `shelfmark` command on a new or
upgraded database applies pending migrations before it does its work. Commands that fail print a
message to standard error and return exit code 1; success returns 0.

Passwords are never taken from the command line. `shelfmark user create` and `shelfmark user
passwd` read the password from the terminal, or from standard input when piped, because an
argument would be visible in `ps` and in shell history.

The full command tree:

```
shelfmark key     create|list|revoke
shelfmark user    list|create|passwd|disable|enable|role
shelfmark enrich  (--all | --ipn IPN) [--force]
shelfmark bom     import|list
shelfmark label   scan|receive
shelfmark offers  refresh|audit
shelfmark subs    list|add|remove
shelfmark build   plan|commit|list|export
```

## API keys: `shelfmark key`

Keys are `smk_` plus 40 hex characters. The plaintext is printed once at creation and is not
retrievable afterwards.

### `shelfmark key create --name NAME --role ROLE`

Creates a key and prints its plaintext. `--role` is one of `viewer`, `engineer`, `admin`.

Writes: one row in the `api_key` table (only the SHA-256 hash is stored).

```
shelfmark key create --name ci --role engineer
```

### `shelfmark key list`

Lists every key: id, name, prefix, role, created_at, last_used_at, revoked_at. The plaintext key
and the hash are never shown.

```
shelfmark key list
```

### `shelfmark key revoke --name NAME`

Revokes the key with that name. A revoked key stops working on the next request. Revoking is
idempotent.

```
shelfmark key revoke --name ci
```

## Accounts: `shelfmark user`

### `shelfmark user list`

Lists accounts: id, username, role, state (`active` or `disabled`), created_at, last_login_at.

```
shelfmark user list
```

### `shelfmark user create USERNAME --role ROLE`

Creates an account. `--role` is one of `viewer`, `engineer`, `admin`. The password is read from
the terminal (typed twice) or from standard input when piped.

```
shelfmark user create sydney --role admin
```

### `shelfmark user passwd USERNAME`

Sets a new password (read from stdin). This also ends every existing session for the user, so a
stolen session dies with the old password.

```
echo "a-new-password" | shelfmark user passwd sydney
```

### `shelfmark user disable USERNAME`

Disables an account. Refuses to disable the last active admin.

```
shelfmark user disable alex
```

### `shelfmark user enable USERNAME`

Re-enables a disabled account.

```
shelfmark user enable alex
```

### `shelfmark user role USERNAME ROLE`

Changes the account's role. Refuses to demote the last active admin.

```
shelfmark user role alex engineer
```

## Enrichment: `shelfmark enrich`

Runs the DigiKey enrichment queue in process. Each queued part has its DigiKey product details
fetched, which fills in the manufacturer, package, distributor links, datasheet and image. Jobs
with no DigiKey credentials configured stay queued and report "credentials not configured".

### `shelfmark enrich --all [--force]`

Queues every part (in IPN order) and runs the queue. Parts that already have a completed job are
skipped unless `--force` is given.

### `shelfmark enrich --ipn IPN [--force]`

Queues and runs a single part. Fails with exit 1 if the IPN does not exist.

```
shelfmark enrich --ipn SK-00001
shelfmark enrich --all
shelfmark enrich --all --force
```

## BOM import: `shelfmark bom`

### `shelfmark bom import --project NAME --file CSV --label LABEL [--note NOTE] [--dry-run]`

Imports a KiCad BOM CSV into a project, creating the project if it does not exist. Prints the
revision id and a diff summary. `--dry-run` parses and matches but writes nothing, printing the
full diff as JSON. Re-importing a byte-identical file fails with exit 1 and names the existing
revision.

```
shelfmark bom import --project sk-sensor --file BOM.csv --label "rev C"
shelfmark bom import --project sk-sensor --file BOM.csv --label "rev C" --dry-run
```

### `shelfmark bom list --project NAME`

Lists a project's revisions newest first: id, label, line count, unmatched count, imported_at and
source filename. Fails with exit 1 if the project does not exist.

```
shelfmark bom list --project sk-sensor
```

## Label scan: `shelfmark label`

Decodes a photo of a DigiKey or Mouser shipping label (JPEG, PNG or WebP, up to 20 MB).

### `shelfmark label scan --file IMG`

Decodes the label and prints JSON with three parts: `payloads` (every raw symbol read), `label`
(the parsed fields), and `resolution` (whether and how it matched an existing part). Writes
nothing to the database.

```
shelfmark label scan --file label.jpg
```

### `shelfmark label receive --file IMG --location LOC [--quantity N] [--note NOTE] [--manufacturer MFR]`

Decodes the label, resolves it to a part (creating one if the label does not match and
`--manufacturer` is supplied), and records a `receive` stock move at the given location. The
quantity defaults to the quantity printed on the label. Prints JSON with the part and the move.
The move note carries the lot and date code from the label when present.

```
shelfmark label receive --file label.jpg --location "Drawer A"
shelfmark label receive --file label.jpg --location "Drawer A" --quantity 25 --manufacturer "Texas Instruments"
```

## Offers: `shelfmark offers`

### `shelfmark offers refresh (--ipn IPN | --all) [--stale-days N]`

Fetches DigiKey price ladders. `--all` refreshes every part whose DigiKey ladder was fetched more
than `--stale-days` ago (default 7); a failure on one part does not stop the run. Prints one line
per refreshed part.

```
shelfmark offers refresh --ipn SK-00001
shelfmark offers refresh --all --stale-days 7
```

### `shelfmark offers audit`

Walks every price ladder and reports each consecutive break pair where buying more costs less in
total (an inverted ladder). Prints the part, MPN, distributor, SKU and the two breaks. Prints
"no inverted price breaks" when there are none.

```
shelfmark offers audit
```

## Substitutions: `shelfmark subs`

### `shelfmark subs list IPN`

Lists the parts that may be used where this part is called for: substitute IPN, fit, substitute
MPN, substitute on hand, creator and note.

```
shelfmark subs list SK-00001
```

### `shelfmark subs add IPN SUBSTITUTE --fit FIT [--note NOTE]`

Adds a directed substitute link. `SUBSTITUTE` is an IPN or an MPN. `--fit` is one of `exact`,
`equivalent`, `form-fit`, `function`. A `form-fit` link requires a non-empty `--note`.

```
shelfmark subs add SK-00001 SK-00012 --fit equivalent --note "same 2.2uF 10V X7R"
```

### `shelfmark subs remove IPN SUBSTITUTE`

Removes the directed link.

```
shelfmark subs remove SK-00001 SK-00012
```

## Builds: `shelfmark build`

### `shelfmark build plan REVISION_ID --boards N [--note NOTE]`

Plans a build of N boards against a BOM revision. Prints the build id and, per line, the IPN, MPN,
required, on hand and short. Writes no stock rows.

```
shelfmark build plan 2 --boards 10
```

### `shelfmark build commit BUILD_ID`

Commits a planning build, consuming stock for every line in one transaction. Fails with exit 1 if
the build is short (nothing is written) or if the build is not in the `planning` state.

```
shelfmark build commit 7
```

### `shelfmark build list`

Lists every build newest first: id, project, revision label, board count, status and created_at.

```
shelfmark build list
```

### `shelfmark build export BUILD_ID --out DIR`

Writes three files into `DIR` (created if needed): `picklist.csv`, `board.xml` and `job.xml`. The
`board.xml` is omitted with a warning when the revision has no positions.

```
shelfmark build export 7 --out ./openpnp
```
