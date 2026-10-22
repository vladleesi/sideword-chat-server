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
| Close a conversation | Blocks new participants and live sending, queued delivery, viewing/read updates and presence through every session. Keeps memberships, the roster, saved local history and pending messages. Other conversations remain usable. |
| Reopen a conversation | Resumes live access through otherwise valid sessions and invites. Retained queued messages may be delivered before their ordinary TTL expires. Does not restore revoked/expired invites or sessions. |
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

Hostnames that route to the same backend can share rooms without sharing browser
identities. A new origin creates its own device; joining needs a usable invite
and consumes a participant slot. A full two-person room cannot admit a third
device, even if its display name matches an existing participant. Names do not
merge identities. Returning to an origin restores its saved device and history;
changing origins neither transfers history nor recovers expired/revoked access.
New participants do not receive messages previously addressed to other identities.

The client closes its live socket on offline/navigation events and reconnects on
network recovery or cached-page restoration. Connection/authentication waits and
HTTP response waits are bounded; failed sockets retry with capped randomized
backoff. Polling and the immutable encrypted outbox remain recovery paths.
An initialized live connection or locally rendered message does not establish
remote delivery; see [confirmation semantics](API.md#durable-delivery-and-actual-viewing).

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

Invite expiry and revocation invalidate sessions issued through that invite,
while other valid sessions retain membership. Do not present invite revocation as a guaranteed room
shutdown or participant ban. For a compromised identity, deactivate the user;
for a leaked admission/resume credential, revoke its invite. To stop messaging
for everyone without deleting data, close the conversation in Admin > Chats.
The client labels it closed, disables sends, pauses its outgoing retries and
keeps local messages readable. Explicit chat deletion remains a separate
destructive action.

Closure uses persistent per-room state enforced on sends, polling, receipts,
backlog, live delivery and presence. Authenticated members can still reconnect
to read their saved archive; new participants cannot join a closed room. Normal
server queue TTL still applies while it is closed. Closing/reopening preserves
the invite's separate revocation/expiry state. It does not silently sign out
sessions that may be needed for other rooms.

Room-specific participant removal remains unimplemented. It would require
updated recipient rosters and preserved local archives. E2EE removal must stop
encrypting future messages to removed participants; it cannot revoke old message
keys. This is separate from conversation closure and invite revocation.

The current test E2EE protocol has no recipient forward secrecy or post-compromise
recovery. A production privacy-focused messenger needs a reviewed ratchet/group
key lifecycle and authenticated membership changes; see
[remaining security limitations](SECURITY_REVIEW.md#threat-model-and-limits).

## Regression coverage

Invite, session, closure and client regression suites exercise authorization,
reconnects, queue preservation and device history with isolated databases and
Node adapters. See the [verification record](SECURITY_REVIEW.md#verification-record-and-gaps)
for results and the remaining device and deployment checks.
