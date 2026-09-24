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
2. Validate code format: exactly 7 chars, alphanumeric, at least one digit. A malformed code responds `400`.
3. Query Synapse DB for rooms whose `m.room.join_rules` state event contains a matching `access_code` (case-insensitive). Uses the latest state event per room.
4. A well-formed code that matches no room responds `404` with `{ errcode: "ORG.PANGEA.CODE_NOT_FOUND" }`. 404 and not 400 because the request is fine — the code doesn't exist; the errcode lets the client tell a wrong code (an expected user mistake, shown as "check the code") apart from a malformed request (a client bug). A whole classroom mistyping one board-written code produced the 2026-08-31 burst that motivated this split (issue #197 / client#8693).
5. For each matched room:
   - If user is already a member → add to `already_joined` list.
   - If user is BANNED from the room → add to `banned` list, skip the invite (Synapse would reject it; without this the failure is indistinguishable from a nonexistent code — issue #127 / client#6820).
   - If user is already INVITED → add to `rooms` without issuing a second invite. The client's own `/join` succeeds for an invited user, so the flow is idempotent across link re-clicks, a second device, and a knock racing the client's join (issue #148).
   - Otherwise → find a room member with invite power, issue `update_room_membership(invite)` on their behalf.
   - An invite that fails — including a room with no eligible inviter — is added to a `failed` list and captured to Sentry. Failures must not block the other matched rooms, but they must not vanish either: the endpoint's own error responses never propagate to Synapse's request-level capture, so an uncaptured failure here is invisible.
6. If the code matched rooms but the user is banned from ALL of them (nothing invited, nothing already joined) → respond `403` with `{ errcode: "ORG.PANGEA.BANNED_FROM_ROOM", error: ..., banned: [...] }` so the client can show a ban-specific message.
7. If every matched room failed (nothing invited, nothing already joined, nothing banned) → respond `500` with `{ errcode: "ORG.PANGEA.INVITE_FAILED", failed: [...] }`. Before this rule, an all-rooms-failed code answered 200 with empty lists, which clients render as "code not found" — hiding a server-side problem behind a user-error message.
8. Otherwise return `{ rooms: [...invited], already_joined: [...], banned: [...] }`.

**Client impact**: The invite arrives via `/sync`. The client's sync listener must suppress the invite dialog when the code flow is already handling the join — see "Space invite priority" in [joining-courses.instructions.md](../../../client/.github/instructions/joining-courses.instructions.md).

### `GET /_synapse/client/pangea/v1/request_room_code`

**Auth**: Bearer token.

**Logic**: Generate a unique 7-char alphanumeric code, verify it doesn't collide with any existing room's code (up to 10 retries), and return `{ access_code: "..." }`. The client stores this code in the room's `m.room.join_rules` state event under the `access_code` key.

**Generation alphabet**: codes are written on whiteboards and retyped by students, so generation excludes every transcription-confusable character — `0/o`, `1/i/l`, `q/g`, `t/y` (all observed in the 2026-08-31 classroom burst, issue #197). Validation still accepts the full alphanumeric set, so codes issued before this decision keep working. The at-least-one-digit rule is unchanged — it is what distinguishes a code from a literal route in the client's URL grammar.

---

## Claiming a course: a private claim link, then the class link

A course created for a teacher who does not yet have an account (the [teacher funnel](../../../.github/.github/instructions/teacher-funnel.instructions.md)'s course request flow) has nobody to administer it: the bot creates the space, so the bot is its only admin. The teacher has to be able to take ownership by following an ordinary link, before they have any standing in the room.

**The claim link and the class link are two codes, and the teacher never holds both at once.** A new course carries a single-use admin code and a class code. The teacher is sent the admin code first, as a link behind a button in the email that tells them their course is ready. The class code reaches them afterwards, in a second email that Synapse sends once the course has been claimed. The first email has nothing in it that belongs with students, so there is nothing to confuse and nothing to warn about.

**Holding the claim link is the proof of identity.** The admin code is sent only to the address the course was requested from, so using it shows control of that inbox. This is deliberately not a match against the teacher's Pangea account: a teacher who requested a course from one address and signs in with another, such as a personal address at a booth and school single sign-on in class, still claims their course. Whoever uses the admin code first becomes the course's admin, and the code is spent. Two people using it at the same moment cannot both become admin: the second is answered as for a code that does not exist, which is what it is a moment later.

**The claim code is kept out of room state.** Every member can read a course's join rules, so an admin code stored there could be read by a student who joined with the class code, and used. A requested course's admin code lives only in a server-side claim record, and that is where this endpoint looks it up. Because nobody can read it from the course, a member who already joined, such as a teacher who tried their class link before opening the claim link, can still claim with it.

**The class code never grants admin.** Anyone who joins with the class code, in any order, joins as an ordinary member. Sharing it early cannot hand the course to a student.

**The second email goes to the requesting address.** It carries the class link, and it is sent to the address the course was created for, not to whoever claimed it, so the teacher receives it even if the claim link was forwarded and used by someone else. It does not name the account that claimed the course (Will, 2026-09-24). It is sent once, and a send that fails is retried in the background, so a mail outage delays it rather than losing it: by then the admin code is spent, and the class link reaches the teacher no other way. A send that stalls is given up on and retried; if the mail server later completes the stalled one, the teacher gets the email twice. That is accepted: the email carries nothing single use, and losing it is worse than repeating it. After it goes out the address is cleared from the record, which keeps who claimed the course and when. A course a teacher created in the client has no such record, so using its admin code promotes and burns as before and sends nothing. The bot keeps full power in every space it creates, so a course claimed by the wrong person can always be repaired server-side.

**Following the link only opens the app; it never claims anything by itself.** University mail filters fetch every link in a message before a person sees it. The claim happens when a signed-in person submits the code in the app, so a filter fetching the link cannot spend the admin code.

Granting admin to someone else later, such as a co-teacher, is a separate deliberate act. The class code is never that path. A new admin code set on a claimed course is that act, not a second claim: the claim record belongs to the admin code the course was created with, so a later one promotes as any admin code does, and sends nothing.

---

## Access Code Storage

A requested course's claim code is the exception: it is not in join rules, for the reason in "Claiming a course" above. Everything below describes the class code and client-set admin codes.

Access codes live in the `content.access_code` field of the room's `m.room.join_rules` state event. This is a Pangea-custom extension — the Matrix spec does not define this field. The code is set client-side when a course admin creates or configures a course.

The `get_rooms_with_access_code` query reads directly from the Synapse event tables (`events` + `state_events` + `event_json`) with DB-engine-specific JSON extraction (PostgreSQL `jsonb` / SQLite `json_extract`).

---

## Invite Mechanics

The server needs a real user with invite power to issue the invite (Synapse's `update_room_membership` requires a sender). [`get_inviter_user`](../../synapse_pangea_chat/room_code/get_inviter_user.py) finds a joined member whose power level meets the room's invite threshold. If no such user exists, the room counts as failed (`NoInviterAvailableError`) and follows the failed-room handling above — it is never reported as invited.

---

## Rate Limiting

Per-user in-memory rate limiting. Configurable via module config:

- `knock_with_code_requests_per_burst` (default: 10)
- `knock_with_code_burst_duration_seconds` (default: 60)

Returns HTTP 429 when exceeded.

---

## Future Work

_(No open issues at this time.)_
