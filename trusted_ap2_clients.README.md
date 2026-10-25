# Trusted AP2 Clients Registry

## Purpose

`trusted_ap2_clients.json` is the allowlist of external agent frameworks
authorized to deliver signed AP2 envelopes to the Genesis agents gateway
(`POST /agents/{slug}/run`). Each entry binds a stable `client_id` to a
base64-encoded Ed25519 public key, an algorithm tag, a capability set, and
an `enabled` flag.

## Format

The file is a single JSON object:

- `version` — integer schema version (currently `1`).
- `description` — human-readable summary of the file's role.
- `clients` — array of client records. Required fields per record:
  - `client_id` — short, stable, lowercase identifier (e.g. `cato`).
  - `principal_id` / `tenant_id` — immutable owner identity bound to jobs and artifacts.
  - `name` — display name.
  - `pubkey_b64` — base64 Ed25519 public key (32 raw bytes encoded).
  - `algorithm` — currently always `ed25519`.
  - `capabilities` — list of gateway scopes the client is allowed to call.
  - `enabled` — boolean kill-switch.
  - `added_at` — ISO-8601 UTC timestamp.
  - `notes` — free-form operator notes.

## Current Auth Model

`POST /agents/{slug}/run` verifies the signed envelope (payload + nonce +
RFC3339 timestamp + Ed25519 signature), consumes the nonce, and derives the
principal and scopes from this registry. The shared gateway key remains a
legacy compatibility credential but cannot read AP2-owned rows.

## Continuation Tokens

Successful async submission returns a short-lived, audience-bound
`principal_token` inside the queued JSON response. Pollers send it only as
`X-Genesis-Principal-Token`; job and artifact routes verify expiry, scope,
tenant, and owner. Tokens are never model input or log content.

See `Protocols/VCAP-AP2-Binding-v1.0-draft.md` for the binding spec and
canonical-bytes definition the middleware will use.

## Operational Notes

### Operator acceptance smoke check

From the Genesis repository, an operator can make one harmless, real signed
request with Cato's existing restored vault identity:

```bash
python scripts/ap2_operator_smoke.py
```

The default target is `https://swarmsync-agents.onrender.com` and the default
agent is `genesis-support`. Override the non-secret origin with
`--url https://your-genesis-host` (or `GENESIS_SMOKE_URL`) and use
`--cato-repo PATH` if the Cato repository is not the usual sibling directory.
If current-vault custody is unavailable but an operator-authorized preserved
encrypted vault is known to contain the same AP2 identity, pass
`--vault-path PATH`. This option only changes the encrypted file read; it never
copies, overwrites, creates, or rotates vault/key material.
The command requires HTTPS, uses `mode: live_test` plus
`skip_internal_escrow: true`, and sends no gateway API key or Authorization
header. It exits `0` only when Genesis returns HTTP 200; refusals are emitted as
small JSON objects with an actionable category and exit `1`. It makes up to
three bounded attempts for transient network/HTTP failures, signing a fresh AP2
nonce each time. Successful output includes only the target, UTC time, attempt,
HTTP status, agent, and a non-secret SHA-256 public-key fingerprint.

The command reads `CATO_AP2_PRIVKEY` and `CATO_AP2_PUBKEY` through Cato's vault
API and refuses to create or rotate a missing identity. The vault may prompt for
its password without echo. Neither that password nor private key material, the
signature, request headers, or response body is printed or logged.

- Add new clients by appending a record and bumping nothing else.
- Rotate a *live* key (old private half still held, overlap needed) by adding a
  second record with a new `client_id` suffix (e.g. `cato-2`) and disabling the
  old one rather than mutating it.
- Rotate a *lost* key (vault destroyed, private half unrecoverable) by replacing
  `pubkey_b64` in place and recording the retired key in `notes` +
  `key_rotated_at`. Do NOT keep a disabled record: a disabled entry is still a
  trust anchor for a key nobody controls, one `"enabled": true` edit away from
  being live again. There is no overlap to preserve — the old key cannot sign.
- Never commit private keys here; this file is public-key only.
