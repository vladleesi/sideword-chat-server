# Changelog

## 0.4.0 — 2026-10-06

- Require body-based invite activation and switch the bundled client to keep admission tokens out of activation URLs; remove the path-based activation endpoint.
- Require HTTPS or local loopback for every invite activation, including password-free rooms and legacy token issuance.
- Reject invalid supplied bearer authentication instead of silently allocating an anonymous participant; validate resume credentials before acquiring the admission write reservation.
- Automatically create versioned GitHub tags and releases after CI promotion, using the matching changelog section as release notes.

## 0.3.3 — 2026-09-27

- Open the join form for a different invite URL instead of resuming the previous chat; select the invited room after activation.
- Pause background chat activity while a new invite awaits activation and preserve device keys, encrypted history, and pending sends across invite changes.

## 0.3.2 — 2026-09-27

- Recheck participant and invite validity when issuing renewable sessions, including activation retries and legacy migration across token expiry or sunset.
- Cap renewable session deadlines by invite expiry and report the earlier session deadline in profile access metadata.
- Verify refresh/revocation races and isolated backup recovery, including both signing-key rotation and removal of restored session records.

## 0.3.1 — 2026-09-27

- Reject admin logins verified against a password changed by a concurrent reset, preserving session revocation.
- Enforce the admin session cap atomically across concurrent logins and reclaim expired sessions during login.

## 0.3.0 — 2026-09-27

- Reject malformed non-ASCII CSRF tokens without a server error and accept validated same-origin no-referrer login forms.
- Keep existing chat messages in place during incoming delivery and preserve the scroll position when reading older messages.
- Stabilize the message viewport and composer during sends/read receipts; show pending retries in the sidebar and prevent input focus from scrolling the page.
- Deduplicate message uploads within a bounded retry window and persist an encrypted browser outbox before sending.
- Add admin CSRF protection, persistent login throttling, and revocable sessions.
- Add opt-in renewable client sessions with hashed refresh rotation, replay detection, per-device revocation, and explicit legacy sunset controls.
- Bound request bodies/rates, WebSocket connections/frames, queued ciphertext, receipts, retry metadata, and participant creation; enforce TLS and local-only administration by default.
- Add exact delivery ACK/read endpoints and random delivery IDs; switch browser retries to the safer contract while preserving legacy APIs.
- Atomically consume read messages and create receipts, preventing duplicate receipts from concurrent readers.
- Preserve the browser v1 wire format while separating protocol operations from UI and storage.
- Compute fingerprints locally, pin peer keys, and block unexpected key changes.
- Generate new private keys non-exportable and retain messages when history storage is unavailable.
- Revalidate WebSocket sessions before delivery and while idle; preserve existing authentication methods.
- Disable raw shared-runner access logs and document security limits and protocol migration requirements.

## 0.2.0 — 2026-09-27

- Standardize Sideword Server configuration, Docker resources, and client identifiers.
- Add a shared backend version for health and OpenAPI responses.
- Establish `develop` for changes and checked promotion to `main`.
- Add protected invites, resumable participant slots, and sealed rooms.
- Improve admin invite creation, encrypted password recovery, and live link lists.

## 0.1.0

- Initial backend: invite-based chats, encrypted-message relay, browser test client, administration, and configuration export/import.
