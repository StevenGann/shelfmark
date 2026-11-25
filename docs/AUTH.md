# Authentication: API keys only

Decision, 2026-11-25. Shelfmark is an on-prem LAN service. It has exactly one
authentication mechanism — **API keys** — and no user accounts, passwords,
sessions or SSO. Every surface that can read or write data takes a key: the REST
API, the HTML UI, the CLI and the MCP server.

The reason for one mechanism is not laziness. Two mechanisms means two ways to be
wrong, two places for a role to drift, and a UI that can be operated by anyone
who finds the hostname while the API is properly guarded. A key in a header and a
key in a cookie are the same key.

## Key format

```
smk_<40 hex characters>        e.g. smk_9f2c... (generated with secrets.token_hex(20))
```

The `smk_` prefix survives log redaction and greps; the rest does not. Only a
SHA-256 hash is stored. The plaintext key is shown once, at creation, and never
again — losing it means revoking it.

## Storage

```sql
CREATE TABLE api_key (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,          -- 'laptop', 'ci', 'mcp-hermes'
  key_hash TEXT NOT NULL UNIQUE,      -- sha256 hex of the full key
  prefix TEXT NOT NULL,               -- first 8 chars after 'smk_', for the UI
  role TEXT NOT NULL CHECK (role IN ('viewer','engineer','admin')),
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT
);
CREATE INDEX api_key_hash_idx ON api_key(key_hash);
```

`last_used_at` is written at most once a minute per key — an every-request write
would turn a read-only query into a write transaction.

## Presenting a key

Both accepted, because integrators expect both:

```
Authorization: Bearer smk_...
X-API-Key: smk_...
```

Comparison is constant-time over the hash. A missing key is `401` with
`WWW-Authenticate: Bearer`; a valid key without the required role is `403`.

## Roles

- `viewer` — read everything.
- `engineer` — viewer plus create/edit parts, ingest BOMs, record substitutions, move and consume stock.
- `admin` — engineer plus release revisions, manage keys, change supplier credentials, budgets and commodity contracts.

## Bootstrapping

On first start, if `api_key` is empty:

1. If `SHELFMARK_BOOTSTRAP_KEY` is set, that value becomes the first admin key.
2. Otherwise one is generated, written to `$SHELFMARK_DATA_DIR/bootstrap-key.txt`
   with mode 0600, and printed to the log **once**, prefixed `BOOTSTRAP KEY`.

The file is not deleted automatically: deleting it is the operator's signal that
they have stored the key somewhere better.

Key management is `admin` only:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/keys` | List: id, name, prefix, role, created, last used, revoked — never the hash or the key |
| POST | `/api/keys` | `{name, role}` → 201 with the plaintext key, once |
| POST | `/api/keys/{id}/revoke` | Sets `revoked_at`; idempotent |

Also `shelfmark key create|list|revoke` for the operator who cannot reach the UI.

## The HTML UI

`/login` takes a key, not a password. On success it is stored in a cookie:
`HttpOnly`, `SameSite=Lax`, `Secure` when the request arrived over TLS, signed
with `SHELFMARK_SECRET_KEY`. The cookie carries the key id, not the key itself,
and the role is re-read from the database on each request so a revocation takes
effect immediately.

Logout deletes the cookie. There is no session table and nothing to expire
server-side; a revoked key is a dead cookie.

## Rate limiting

Per-key fixed window, in memory, 600 requests per minute (`SHELFMARK_RATE_LIMIT`
to change, 0 disables). Over the limit is `429` with `Retry-After`. The point is
to stop a looping integration from starving the UI, not to defend against an
attacker who already holds a key.

## Credentials at rest

Supplier credentials (DigiKey client id and secret) are entered in the UI and
stored encrypted with `SHELFMARK_SECRET_KEY` (Fernet, key derived from it), never
in the image, the repo or the export. The export strips them by default.

## MCP

`shelfmark mcp` runs a **stdio** MCP server exposing read-only tools
(`parts_search`, `part_get`, `stock_for_part`, `build_shortage`, `revision_lines`).
It opens the SQLite database directly through the same service layer as the REST
routes — no HTTP, no second query path, no key required because possession of the
process is the authorisation. It is read-only by construction: the tool functions
have no write path to call.

Wiring it into an agent is a config entry, not a deployment:

```json
{"mcpServers": {"shelfmark": {"command": "docker",
  "args": ["exec", "-i", "shelfmark", "shelfmark", "mcp"]}}}
```

## What this is not

Not a user directory, not a permissions matrix, not a tenancy model. If Symbio
ever needs per-person accounts, this table gains a `username` column and the
mechanism stays the same — which is the test of whether the simple version was
built in the right direction.
