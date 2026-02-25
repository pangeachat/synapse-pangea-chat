---
applyTo: "synapse_pangea_chat/room_code/**"
---

# Room Code — knock_with_code & request_room_code

Two Pangea-custom Synapse endpoints that let users join knock-only courses with an access code, bypassing the standard knock → admin-approve flow.

- **Client-side joining flow**: [joining-courses.instructions.md](../../../client/.github/instructions/joining-courses.instructions.md) (Routes 1 & 2)

---

## Design Decision

Standard Matrix knock requires an admin to manually approve every join request. For class links and class codes, we want instant access — the code _is_ the authorization. Rather than changing the room's join rule (which would remove the admin gate for codeless users), these endpoints let the server invite the user directly when a valid code is presented.

**This is NOT a Matrix knock.** The name `knock_with_code` is a historical misnomer. The endpoint validates the code and issues a server-side invite — the user never enters `Membership.knock` state.

---

## Endpoints

### `POST /_synapse/client/pangea/v1/knock_with_code`

**Auth**: Bearer token (standard Matrix auth).

**Request**: `{ "access_code": "<7-char alphanumeric string>" }`

**Logic**:

1. Rate-limit check (configurable burst window per user).
2. Validate code format: exactly 7 chars, alphanumeric, at least one digit.
3. Query Synapse DB for rooms whose `m.room.join_rules` state event contains a matching `access_code` (case-insensitive). Uses the latest state event per room.
4. For each matched room:
   - If user is already a member → add to `already_joined` list.
   - Otherwise → find a room member with invite power, issue `update_room_membership(invite)` on their behalf.
5. Return `{ rooms: [...invited], already_joined: [...], rateLimited: false }`.

**Client impact**: The invite arrives via `/sync`. The client's sync listener must suppress the invite dialog when the code flow is already handling the join — see "Space invite priority" in [joining-courses.instructions.md](../../../client/.github/instructions/joining-courses.instructions.md).

### `GET /_synapse/client/pangea/v1/request_room_code`

**Auth**: Bearer token.

**Logic**: Generate a unique 7-char alphanumeric code, verify it doesn't collide with any existing room's code (up to 10 retries), and return `{ access_code: "..." }`. The client stores this code in the room's `m.room.join_rules` state event under the `access_code` key.

---

## Access Code Storage

Access codes live in the `content.access_code` field of the room's `m.room.join_rules` state event. This is a Pangea-custom extension — the Matrix spec does not define this field. The code is set client-side when a course admin creates or configures a course.

The `get_rooms_with_access_code` query reads directly from the Synapse event tables (`events` + `state_events` + `event_json`) with DB-engine-specific JSON extraction (PostgreSQL `jsonb` / SQLite `json_extract`).

---

## Invite Mechanics

The server needs a real user with invite power to issue the invite (Synapse's `update_room_membership` requires a sender). [`get_inviter_user`](../../synapse_pangea_chat/room_code/get_inviter_user.py) finds a joined member whose power level meets the room's invite threshold. If no such user exists, the invite silently fails for that room.

---

## Rate Limiting

Per-user in-memory rate limiting. Configurable via module config:

- `knock_with_code_requests_per_burst` (default: 10)
- `knock_with_code_burst_duration_seconds` (default: 60)

Returns HTTP 429 when exceeded.

---

## Future Work

_(No open issues at this time.)_
