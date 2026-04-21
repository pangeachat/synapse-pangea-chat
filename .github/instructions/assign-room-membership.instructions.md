---
applyTo: "synapse_pangea_chat/assign_room_membership/**,synapse_pangea_chat/__init__.py,tests/test_assign_room_membership_e2e.py"
---

# Assign Room Membership — Synapse Module

Assigns one or more local users to a room through a server-admin endpoint without requiring the caller to already be a room member.

## Endpoint

`POST /_synapse/client/pangea/v1/assign_room_membership`

Lives in the `assign_room_membership/` sub-package.

## Contract

- **Auth**: Server admin only.
- **Request**: `{ "room_id": "!room:example.com", "user_ids": ["@user:example.com"], "force_join": true }`
- **Validation**:
  - `room_id` must be a valid room ID.
  - `user_ids` must be a non-empty array of distinct local user IDs.
  - `force_join` must be a boolean.

## Behavior

- Caller membership in the target room is not required.
- `force_join: false`
  - invite each target user unless they are already invited or already joined.
  - do not complete the join on the user's behalf.
- `force_join: true`
  - ensure each target user is joined.
  - if the room's join rules require it, the endpoint may invite first and then complete the join.
- Partial success is part of the contract. One user's failure must not prevent attempts for the remaining users.

## Per-User Results

- Results are returned in request order.
- Each result reports the target `user_id`, whether the assignment succeeded, and the primitive action outcome.
- Successful actions are limited to `invited`, `joined`, `already_invited`, and `already_joined`.
- Failures use `action: failed` plus an `error` string so the caller can retry or surface the failure.

## Non-Goals

- Power-level changes.
- `m.direct` repair or DM-specific orchestration.
- Access-token minting or returning access tokens.
- Remote or federated target users.
- Automatic unban or other membership-state repair beyond room assignment.

## Future Work

_Last updated: 2026-04-21_

**Bot rollout**

- [pangeachat/pangea-bot#1159](https://github.com/pangeachat/pangea-bot/issues/1159) — Integrate room-assignment endpoint in join_and_play and analytics access
- [pangeachat/pangea-bot#1142](https://github.com/pangeachat/pangea-bot/issues/1142) — Audit bot impersonation call sites and propose Synapse admin endpoint workaround

**Impersonation cleanup**

- [pangeachat/pangea-bot#1150](https://github.com/pangeachat/pangea-bot/issues/1150) — Remove generic user access token minting after impersonation migrations land