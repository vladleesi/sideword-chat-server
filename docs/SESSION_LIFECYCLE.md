# Invite, session and conversation lifecycle

Invites authorize admission; sessions authenticate an identity; memberships
allow conversation access. One identity can belong to several chats.

Names belong to memberships: the name supplied when first joining a room is used
only in that room. Joining another room can use a different name; retries and
reconnects retain the room's original name. These are unverified labels, not
separate identities: the same public ID and key remain visible across those rooms.

| Operation | Effect |
| --- | --- |
| Open an invite | Shows admission UI without taking a slot; a different invite pauses that tab until activation. |
| Activate with a valid session | Reuses its identity after matching the key; a new protected room still requires its password. |
| Activate anonymously | Creates an identity unless that invite's saved resume credential and matching key recover its participant. A public key alone is insufficient. |
| Consume/seal an invite | Stops new joins; authenticated reconnects and valid sessions continue. |
| Revoke/expire/delete an invite | Blocks admission, resume, HTTP/WS auth and refresh through that invite. Keeps memberships; other valid sessions retain access. |
| Revoke/expire a session | Blocks that session, not others or its invite resume credential. Refresh never extends absolute lifetime. |
| Deactivate a user | Blocks the identity across chats, including resume; reactivation may restore unexpired access. |
| Delete a user | Removes its server identity, memberships and relay data. Other identities are separate. |
| Close a conversation | Blocks new joins, sends, delivery, viewing/status updates and presence for everyone. Preserves memberships, queues and local archives. |
| Reopen a conversation | Resumes authorized access and retained delivery within TTL; restores no revoked/expired credentials. |
| Delete a chat | Removes server conversation/memberships/relay data and tombstones invites; cannot erase participants' copies. |

## Membership and revocation

`/api/v1/me` lists every membership for the authenticated identity, regardless of
which invite issued its current session. If an identity joined A and B, revoking
A's invite does not remove A membership: its valid B session still accesses A.
To stop everyone, close A; to block the identity across chats, deactivate the user.
Room-specific participant removal is not implemented.

If A was the only issuing invite, its revocation blocks that session and refresh.
An unrelated invite cannot recover A's membership. Retrying without the rejected
bearer may create a different participant; saved history stays under the old ID.
Invalid supplied authentication is rejected, never silently treated as anonymous.

Closure retains queues under ordinary TTL and permits late exact durable ACKs,
own-outbox deletion and archive reconnects by existing members. It does not sign
out sessions needed for other rooms. See the [API](API.md#profile-and-chats) for endpoints.

## Persistence, devices and reconnects

The test client stores non-exportable identity/history keys, sessions and per-invite
credentials in origin-scoped IndexedDB; history/outboxes are encrypted and scoped
by participant ID. Invite-vault data uses per-tab session storage. Same-origin
profile tabs share device state and coordinate rotation; another browser, profile,
device or origin has independent keys and needs its own participant slot.
There is no key/history synchronization. Names do not merge identities, and new
participants receive no earlier messages addressed to someone else.

Aliases can share rooms only when HTTP and WS reach one process/database; they
share no browser storage. A full personal room cannot admit a third device.
Returning to an origin restores its saved state without restoring revoked access.
Clearing site data/resetting the device can permanently lose keys/history and does
not free a slot. Preserve state when handling authentication/storage errors.

HTTP checks current user, invite, session and deadlines. WS revalidates before
pushes, on incoming frames and periodically while idle; independent workers learn
remote changes at validation time. Checks cannot retract already-sent data or
eliminate all check/send races. Bounded reconnects, polling and persisted identical
outbox retries recover interrupted connections; a live socket is not delivery proof.

Revocation cannot erase plaintext, keys or screenshots. A cached view is not live
authorization. Encryption and first-use trust limits are defined in the
[protocol](PROTOCOL.md); retention/recovery rules are in [UPGRADING.md](UPGRADING.md).
