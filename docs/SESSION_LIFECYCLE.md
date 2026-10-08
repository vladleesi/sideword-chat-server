# Invite, session and conversation lifecycle

Invites authorize admission; sessions authenticate a participant identity;
memberships authorize access to conversations. These are separate records.
This application intentionally allows one identity to belong to several chats.

| Operation | Current behavior |
| --- | --- |
| Open an invite | Shows admission UI without consuming a slot. A different invite pauses synchronization in that tab until activation. |
| Activate with a valid existing session | Reuses its identity after matching the public key; requires the password for a new protected room. All existing memberships remain accessible. |
| Activate without a valid session | Creates a new identity, unless that invite's saved resume credential authenticates an existing participant with the matching key. A public key alone never restores membership. |
| Consume/seal an invite | Stops new admissions; existing authorized sessions and authenticated reconnects remain valid. |
| Revoke, expire or delete an invite | Blocks activation/resumption and HTTP/WS authentication and refresh through that issuing invite. Does not remove memberships or terminate its chat. Another valid session for the same identity retains access to all its memberships. |
| Revoke/expire a session | Invalidates that session, not other sessions or invite resume credentials. Session refresh does not extend its absolute lifetime. |
| Deactivate a user | Blocks that identity's sessions, refresh and invite resumption across chats. This is account-wide, not removal from one room. Reactivation can restore unexpired access. |
| Delete a user | Explicit destructive administration removes the identity and associated membership/relay data. Other identities on the same device are separate. |
| Delete a chat | Explicit destructive administration removes the server conversation, memberships and associated relay records and tombstones its invites. It cannot erase participants' local history or copies. |

## Why an old chat can appear after a new invite

`activate_link` reuses `current_user` only after validating supplied bearer
authentication. `/api/v1/me` returns all `ChatMember` records for that identity,
regardless of which invite issued the current session. The client renders this
server roster and merges encrypted IndexedDB history scoped by participant public
ID and chat ID. Switching invites is intentionally not a history wipe.

For example, an identity joins rooms A and B before A's invite is revoked. Its
session through B is still valid. Activating C with that session returns the same
identity and lists A, B and C; it can still exchange messages in A. This is
authorized membership access, not reactivation of A's invite. Each participant's
ability to reconnect depends on their own identity and valid credentials.

If A was the identity's only issuing invite, revoking it rejects its old bearer
and refresh credentials. The client displays an error and preserves keys and
history. Explicitly retrying B without that bearer creates a new participant;
B's unrelated resume credential cannot recover A's membership. The old history
remains stored under the old public ID, and is not listed as the new participant's
conversation. Opening a new link alone never authorizes old-chat access.

The reported sequence cannot be explained by the current implementation using
only a revoked session and an unrelated fresh invite. Another valid session or
a different deployed implementation would need to be established. No deployed
instance, Windows Chrome session or historical runtime state was inspected.

## Persistence, devices and reconnects

The bundled client stores its non-exportable P-256 private key, separate
non-exportable AES history key, current session, and per-invite admission
credentials in origin-scoped IndexedDB. History and outbox contents are encrypted
and scoped by public ID. Theme storage is separate; it does not authorize chats.
The invite vault keeps protected invite data in per-tab session storage. HTTP
responses for invites and authenticated APIs are no-store; cached public scripts
or styles do not grant membership.

Tabs in the same browser profile and origin share IndexedDB/session state;
credential rotation uses device locks and atomic persistence. A new invite pauses
only its tab. Other browsers, profiles, private browsing contexts, devices or
origins do not automatically share these keys or credentials. Public keys or an
invite URL alone cannot reconstruct a joined identity or decrypt stored history.
The bundled client has no cross-device history/key synchronization.

HTTP checks the live user, issuing invite, token/session deadlines and session
revocation. Chat operations additionally check membership; polling and WebSocket
backlog deliver ciphertext addressed to the authenticated identity. WebSockets
revalidate each connection before delivery, on frames and periodically while idle.
Admin invite revocation rechecks affected connections individually, preserving
sessions issued through other valid invites. Reconnecting never bypasses these
checks. Authorization cannot retract a response already sent or remove every
check/send race. Independent workers observe remote revocation at their next
validation rather than through a shared push registry.

## Recommended privacy model and operational limits

Keep local history until an explicit user action deletes it. A cached plaintext
view is not evidence of live authorization; successful fresh HTTP sends and
recipient delivery are. Revocation cannot erase plaintext, keys or screenshots
already held by participants. Reset device/clearing site data can permanently lose
access and history; do not use it to fix invite errors.

Keep admission, identity authentication and conversation authorization separate,
and make their consequences explicit. Signal similarly offers separate controls
to [disable group links](https://support.signal.org/hc/en-us/articles/360051086971-Group-Link-or-QR-code)
and [remove group members](https://support.signal.org/hc/en-us/articles/360050427692-Manage-a-group).
Validate authorization on every operation, following
[OWASP guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html).

Preserve the existing invite-bound session policy for compatibility: expiry and
revocation invalidate sessions issued through that invite, while other valid
sessions retain membership. Do not present invite revocation as a guaranteed room
shutdown or participant ban. For a compromised identity, deactivate the user;
for a leaked admission/resume credential, revoke its invite. For ending a room,
the current admin control is explicit chat deletion, with server data loss.

Room-specific participant removal and non-destructive conversation termination
are not implemented. If added, they need explicit per-room authorization state
enforced on send, polling, receipts, backlog and live delivery, updated recipient
rosters, and preserved local archives. E2EE removal must stop encrypting future
messages to removed participants; it cannot revoke old message keys. A frozen
room should preserve history and reject new sends without deleting data. These
are separate features, not implicit effects of revoking an invite.

The current test E2EE protocol has no recipient forward secrecy or post-compromise
recovery. A production privacy-focused messenger needs a reviewed ratchet/group
key lifecycle and authenticated membership changes; see
[remaining security limitations](SECURITY_REVIEW.md#threat-model-and-limits).

## Regression coverage

`tests/test_invite_lifecycle.py` covers revoked/expired issuing credentials,
legacy and renewable authentication, saved resume/refresh rejection, unrelated
invites, both participants exchanging old-room messages through other authorized
sessions, preserved membership/queued ciphertext, and admin revocation with mixed
valid/invalid sockets and reconnects. `tests/client_retry.test.cjs` covers the
new-invite transition after a rejected login, roster isolation and preserved
device keys/history. Existing session, presence, delivery and crypto suites cover
session expiry/replay, membership boundaries, local encrypted persistence and
non-exportable keys. These are isolated automated checks, not browser/device or
production deployment evidence.
