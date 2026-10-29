# Invite, session and conversation lifecycle

Invites authorize admission, sessions authenticate an identity, and memberships grant
chat access. One identity can join several chats. Names are unverified membership labels;
public IDs and keys remain shared across that identity's rooms.

| Action | Effect |
| --- | --- |
| Open an invite | Shows admission UI; consumes no slot. A different invite pauses that tab until activation. |
| Activate | Valid session/key or saved invite resume credential/key reuses the identity. Otherwise creates one; a public key alone cannot recover it. New protected rooms require their password. |
| Consume/seal invite | Stops new joins; authorized reconnects/refresh continue. |
| Revoke/expire/delete invite | Blocks admission, resume and sessions issued through it. Keeps memberships. |
| Revoke/expire session | Blocks that session; other sessions and invite resume credentials survive. |
| Deactivate/delete user | Blocks the identity across chats; deletion also removes server identity/memberships/relay data. |
| Close chat | Blocks joins, sends, delivery, status/viewing and presence. Retains memberships, queues and local history. |
| Reopen chat | Resumes authorized delivery within original deadlines; restores no invalid credentials. |
| Delete chat | Removes server chat/memberships/relay data and tombstones invites; cannot erase local copies. |

## Membership and revocation

Sessions access all of their identity's memberships. Revoking room A's invite does
not remove A membership: a valid session from room B still accesses A. Close A to
stop everyone; deactivate a user to block that identity. Room-specific removal is absent.
Invalid supplied authentication is rejected, never treated as anonymous admission.
Room names survive reconnects; a new room can use a different name.

Closed chats allow archive reconnects, late exact durable ACKs and own-outbox deletion.
Queue expiry continues during closure. Session refresh never extends absolute lifetime.

## Persistence, devices and reconnects

Browser origins/profiles/devices have independent non-exportable keys and participants;
there is no key/history sync. Same-origin tabs share device state and coordinate refresh.
IndexedDB history/outboxes are encrypted; invite-vault data uses per-tab session storage.
New participants receive no old messages addressed to others. Resetting/clearing browser
state can permanently lose keys/history and does not free a participant slot.

Authenticate, decrypt and persist before ACK. Failed validation/storage remains retryable;
downloading is optional and viewing confirms only the attachment card. Downloads are
ordinary local files that server deletion/revocation cannot erase.

HTTP and WS check live authorization; WS also revalidates while idle/before pushes.
Checks cannot retract already-sent data or eliminate every check/send race. Polling and
identical persisted outbox retries recover interruptions. Ordinary restarts retain pending
content; supported backup restores omit it. Aliases must reach one process/database but
share no browser state. See [protocol](PROTOCOL.md) and [recovery](UPGRADING.md).
