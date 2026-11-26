# Authentication

Two mechanisms, two audiences, one role ladder.

- **API keys** (`smk_…`) for machines: scripts, integrations and the CLI
  server. Sent as `Authorization: Bearer …` or `X-API-Key: …`.
- **Username and password** for people: the HTML UI, logged in once and carried
  by a server-side session cookie.

There is no third mechanism. No OAuth, no SSO, no LDAP — this is an on-prem LAN
service and the deployment is one process in one container.

Roles are `viewer < engineer < admin` and mean the same thing for both
mechanisms.

## API keys

- Format `smk_` + 40 hex characters (20 random bytes). Shown once, at creation.
- Stored as a SHA-256 hash. `prefix` (the first 8 hex characters) is stored for
  display and is never sufficient to authenticate.
- Revoking is immediate: the lookup happens per request and a revoked key is a
  `401`, not a `403`, so revoking cannot be distinguished from never having
  existed.
- `last_used_at` is stamped at most once a minute per key, so a read-only
  request does not become a write.
- Bootstrap: when the `api_key` table is empty the service creates one admin key,
  writes it to `$SHELFMARK_DATA_DIR/bootstrap-key.txt` (0600) and logs only its
  prefix. `SHELFMARK_BOOTSTRAP_KEY` supplies it instead when set.

## People

- `app_user`: `username`, `username_norm`, `password_hash`, `role`, `created_at`,
  `last_login_at`, `disabled_at`.
- Passwords are hashed with **scrypt** from the standard library
  (`n=2**15, r=8, p=1, dklen=32`, 16-byte random salt), stored as
  `scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`. Verified with
  `hmac.compare_digest`. Parameters live in the stored string so they can be
  raised later and rehashed on the next successful login.
- Minimum length 12, no composition rules, no expiry, no security questions. A
  password equal to the username is refused.
- A disabled user is `401` on the next request and all their sessions are deleted.

### Sessions

- `user_session`: `token_hash`, `user_id`, `created_at`, `last_seen_at`,
  `expires_at`. The cookie carries a random 32-byte token; only its SHA-256 is
  stored, so a database read does not yield a usable cookie.
- Cookie `smk_session`: `HttpOnly`, `SameSite=Lax`, `Secure` when the request is
  https, 30-day sliding expiry. `last_seen_at` is updated at most once a minute.
- Changing a password deletes every other session for that user.
- Logout deletes the session row; the cookie is cleared as well.

### First run

When `app_user` is empty the UI serves `/setup` to create the first admin
account, and every other UI route redirects there. Once one user exists `/setup`
returns 404. The setup form is claimable by anyone who reaches the instance
before its owner does, so a fresh deployment should be claimed immediately —
that is the trade for having no installer step and no printed password.

## Rate limiting

In-memory fixed window, per process, which is correct because the container runs
one worker by contract.

- API keys: 600 requests a minute (`SHELFMARK_RATE_LIMIT`, 0 disables).
- Unauthenticated requests: a tenth of that, bucketed by client IP.
- Logins: 5 failures per (ip, username) per 15 minutes, then `429` with
  `Retry-After`. The failure response is always the same sentence, whether the
  username exists or the password is wrong.

## MCP

There is none. An earlier draft of this document described a read-only stdio
server, `shelfmark mcp`; no such command was ever built. `shelfmark --help` is
the authority on what ships, and it lists eight commands.
