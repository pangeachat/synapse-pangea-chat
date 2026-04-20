---
applyTo: "synapse_pangea_chat/direct_message/**,synapse_pangea_chat/__init__.py,tests/test_direct_message_e2e.py"
---

# Ensure Direct Message — Synapse Module

Creates or repairs a 1:1 DM room between two local Matrix users without requiring either user's access token.

## Endpoint

`POST /_synapse/client/pangea/v1/ensure_direct_message`

Lives in the `direct_message/` sub-package.

## Contract

- **Auth**: Server admin only. The endpoint rejects non-admin callers.
- **Request**: `{ "user_ids": ["@alice:example.com", "@bot:example.com"] }`
- **Validation**:
  - `user_ids` must be an array of exactly 2 entries.
  - Both entries must be distinct local user IDs.
- **Response**: Returns the DM room ID plus whether the room was created or reused, and whether `m.direct` was updated for each participant.

## Behavior

The endpoint guarantees a client-usable DM for the exact two local users in `user_ids`.

- If an existing 1:1 DM already exists for that pair, reuse it.
- If a reusable room exists but is missing `m.direct` for one or both users, repair `m.direct` for both users.
- If no reusable room exists, create a private direct room for the pair and then write `m.direct` for both users.
- The result must behave as a DM in clients from both participants' perspectives, not just the caller's.

## Reuse Rules

- Reuse only a room that is clearly a DM for the same two users.
- Group rooms, rooms with extra joined members, and rooms that are only incidentally shared by the two users do not qualify.
- If multiple qualifying rooms exist, choose one deterministically and repair it rather than creating yet another DM.

## `m.direct`

- `m.direct` is part of the contract, not a best-effort side effect.
- The endpoint must ensure the room appears in both users' `m.direct` account data under the other participant's user ID.
- Repair is additive: preserve unrelated `m.direct` entries.

## Intended Use

- Primary consumer: the bot and other server-admin workflows that need a DM without minting user access tokens.
- The endpoint may create DMs for any two local users, not just between the caller and a target user.

## Non-Goals

- Remote/federated users.
- Creating multi-user chats.
- Cleaning up duplicate legacy DM rooms beyond choosing one room to reuse.

## Future Work

### Bot integration

- [pangeachat/pangea-bot#1142](https://github.com/pangeachat/pangea-bot/issues/1142) — replace bot impersonation-based DM creation with server-admin endpoint calls.

### Direct-message hygiene

- [pangeachat/synapse-pangea-chat#75](https://github.com/pangeachat/synapse-pangea-chat/issues/75) — current endpoint contract; follow-up cleanup of duplicate DM selection and diagnostics can extend this feature if needed.