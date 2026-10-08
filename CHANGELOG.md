# Changelog

## 0.8.0 — 2026-10-07

- Show ephemeral participant presence and online counts in the test client using authenticated, opt-in WebSocket snapshots; handle multiple connections, reconnects, heartbeat expiry, and unknown status when connectivity is unreliable.
- Scope presence to current conversation memberships without storing activity history; add a presence toggle for deployments with independent workers and preserve legacy WebSocket event streams.

## 0.7.0 — 2026-10-07

- Show sender names beneath outgoing and personal-chat messages, and reader names with public IDs on read receipts; fall back to public IDs for unnamed participants and refresh labels in saved history when the roster is available.
- Prevent selecting the Send button label and refresh client scripts and styles on reload.
- Redesign the test client as a compact messaging workspace with separate mobile conversation views, visible pending retries, and accessible conversation and device dialogs.
- Keep full participant IDs and fingerprints available for comparison and copying, distinguish local pins from identity verification, and show changed-key messaging blocks outside the details dialog.
- Match client activation and invite landing pages to the Sideword visual identity with bundled IBM Plex fonts and persistent Light, Dark, and System appearance preferences; keep admin sign-in unchanged.
- Show a loading state during saved-device restoration so refreshing a chat does not briefly display the invite activation form.
- Compact the mobile client headers with visible Back navigation and labeled settings/details icons, preserving the desktop layout and visible connection/security information.
- Restore the selected mobile conversation and view after refresh, open conversations at their latest messages, and preserve older reading positions during polling and viewport changes.

## 0.6.0 — 2026-10-07

- Replace the test encryption protocol with native non-exportable P-256 ECDH keys, keeping HKDF-SHA-256, AES-256-GCM, local key pinning, encrypted history, and durable delivery safeguards.
- Require validated 65-byte P-256 public points for activation and configuration imports; reject retired device credentials and mixed-curve rooms. Existing test rooms require new invites and identities; the browser starts a separate P-256 device store without deleting prior data.
- Keep the test envelope at v1 and the HTTP API at /api/v1 with an explicit P256 algorithm identifier and a new authenticated context; remove all X25519 encryption support.
- Check that browser device keys can be restored from local storage before invite activation, avoiding admission on incompatible storage while retaining non-exportable keys.
- Report unreadable or missing local device data separately from a device change, preserve unreadable records, and keep startup storage errors visible on the join form.
- Explain browser storage, changed keys, login, invite, and service failures in plain language; show HTTP status for rejected requests and keep raw server validation values and exception text out of client errors.

## 0.5.0 — 2026-10-06

- Show each sender's chosen display name alongside their public ID on incoming group messages, including existing local history when sender routing metadata is available; refresh client assets on reload.
- Open restored chat history at the latest messages after refresh, including when server synchronization is unavailable, while preserving the scroll position when reading older messages.

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
