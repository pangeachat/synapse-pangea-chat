---
applyTo: "synapse_pangea_chat/room_code/**,synapse_pangea_chat/preview_with_code/**"
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
2. Validate code format: exactly 7 chars, alphanumeric, at least one digit. A malformed code responds `400` with `{ errcode: "M_INVALID_PARAM" }`. This check is the only place the format rule lives. The client sends whatever the learner typed without checking it, so a malformed code is usually a learner entering something that isn't a code, such as a course name. The errcode marks it as the learner's input. The other 400s (missing `access_code`, not a string, invalid JSON) carry no errcode, because only a client bug causes them.
3. Find the rooms whose current `m.room.join_rules` carries the code as `access_code` or `admin_access_code`, ignoring case. See "Code Lookup" below.
4. A well-formed code that matches no room responds `404` with `{ errcode: "ORG.PANGEA.CODE_NOT_FOUND" }`. 404 and not 400 because the request is fine — the code doesn't exist; the errcode lets the client tell a wrong code (an expected user mistake, shown as "check the code") apart from a server-side failure. A whole classroom mistyping one board-written code produced the 2026-08-31 burst that motivated this split (issue #197 / client#8693).
5. For each matched room:
   - If user is already a member with an ordinary class code → add to `already_joined`. Process private claims and additional-instructor grants before this shortcut.
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

## Claiming a course

Preparation and operator contracts are governed by [create-course-space](create-course-space.instructions.md).

Preserve the current bare seven-character link and authenticated v1 `knock_with_code` request. Resolve codes across private invitation fingerprints and existing room codes, with shared collision checks. Link fetching and previews never reserve a claimant, create a room or spend a code. For a valid prepared/provisioning invitation, the authenticated v1 code-preview endpoint returns 409 with `ORG.PANGEA.COURSE_NOT_CREATED`, without a fabricated room ID, administrator or state events. After completion, only a replay-authorized winner may use that private code for the existing room preview; unavailable private codes disclose no invitation details. Ordinary room-code previews keep their existing response. The current app's join path does not depend on this preview endpoint.

The first valid authenticated claim reserves the invitation for that account, without matching its account email to the requesting address. Concurrent claims by other accounts cannot create a room or obtain rights. The winning account creates the prepared Matrix course as its joined creator and administrator. Preserve course settings and ordinary class-code permissions. Joining the conversational bot happens separately and cannot gate claiming or ownership.

A prepared invitation can also be claimed without a link by an account that holds its requesting address as a verified email ([create-course-space](create-course-space.instructions.md), "Who the course is created for"). That path runs this same claim, and only the link resumes a partial claim.

Room creation and invitation completion must recover across workers, request timeouts and process crashes. A durable association with the invitation must identify a room created before the final invitation update. A retry recovers that same room; it does not make another. If creation's outcome is uncertain, return a recoverable server error and reconcile before trying to create again. A worker lease expiring does not transfer the claim to another account or justify duplicate creation.

Explicit invitation revocation and deliberate membership removal, leaving or demotion take precedence during partial claims as well as after completion. Recovery must distinguish a provisioning step that never succeeded from later withdrawal of rights; it must not automatically restore rights once withdrawn. If that distinction cannot be established, stop recovery for operator investigation. A server administrator may revoke a stranded invitation or repair its recorded provisioning operation after verifying the evidence. A lease timeout never changes the winning account; assigning someone else requires a separate explicit administrative action.

Complete the claim only after joined membership, instructor rights and required course settings are durable and verified. Return its room in the existing `already_joined` field. Keep `rooms` and `banned` compatible; do not introduce an asynchronous polling response or report successful empty results while provisioning runs. The current client can handle already-joined rooms before local sync catches up, but this requires end-to-end verification on supported apps.

After completion every link for that invitation loses its ability to grant rights. The same winning account may replay to retrieve the existing room only while it still has joined membership and instructor rights. Removal or demotion cannot be undone by an old claim code. Other accounts receive the same response as an unavailable code. A lost success response or repeat submission does not send another share kit.

A missing prepared specification, failed provisioning or failed rights verification is a server error, not a nonexistent code or a completed claim. The winning account retains the ability to resume partial work without a replacement email.

## Codes, share kit and existing courses

Store only private fingerprints of claim codes; do not log raw codes or access tokens. Earlier reminder links work for the same invitation until completion or explicit revocation. The class code is created with the room and grants only ordinary membership. The initial email carries the claim CTA; the class link follows after ownership is established.

Completion durably records that the share-kit email is owed to the original requesting address, even when another account accepted a forwarded link. It does not name that account. Sending uses exclusive reservation and independent retries; an ambiguous mail timeout may duplicate a share kit but must not recreate a course. Clear the requesting address after successful delivery while retaining claim and room references for audit and authorized replay.

Additional-instructor invitations target an existing course and authorize a role grant. They never provision a replacement course, transfer first ownership or send the first-owner share kit. An existing student can accept an authorized instructor grant. Existing-room invitations use a currently eligible joined administrator as sender, which need not be the bot or an online client. Preserve bans, blocks and grant revocation. A room without an eligible administrator requires explicit recovery. Existing client-issued admin codes remain supported; this change does not require a new co-teacher UI.

A server admin prepares one through `POST /_synapse/client/pangea/v2/instructor_invitations` with the request key, the course's room id and the instructor's address. It is the same invitation record as a first claim, with the room known from preparation and `kind` `instructor` in status reads, and it sends no email: the caller sends every instructor-invitation email and follow-up through the v2 claim-reminder endpoint with its own rendered copy, each carrying a fresh claim link. Preparation refuses a room that does not exist (404), a room that is not a course space (400) and a room with no eligible joined local instructor (409, `ORG.PANGEA.NO_ELIGIBLE_INSTRUCTOR`), because the sender has to exist before the invitation does. Repeating unchanged input returns the same invitation; changed input under the same key is a conflict. `GET` on the same path lists instructor invitations by status (prepared unless asked otherwise), newest first, never with the address. Revocation uses the status resource like any other invitation.

Claiming one, by link or by verified address, reserves the invitation for the account exactly as a first claim does, then: a banned account, or one every joined admin has blocked, gets the same answer as a nonexistent code; an account that is not a member is invited by the eligible instructor and joined, while an existing student is promoted in place; the instructor grants admin power; the grant is verified as joined instructor rights before completion. Completion clears the requesting address and records nothing else: no claim record, no class code, no join-rule change, no share kit. A revoked invitation, a demoted winner or another account sees a nonexistent code; a room with no eligible instructor at claim time, or rights withdrawn during a partial claim, answers `ORG.PANGEA.CLAIM_RECOVERY_REQUIRED` and keeps the reservation for the winning account. `knock_with_code` answers the room in `already_joined`. Analytics access follows the role rather than the claim: a student's client grants the course's instructor cohort, read from room power, when it joins a course that requires analytics access ([grant-instructor-analytics-access](grant-instructor-analytics-access.instructions.md)); the claim does not back-fill existing students' analytics rooms, which the operator's grant skill does.


Legacy room-backed claims remain valid. Courses with human members or activity preserve their room identity and history. Empty legacy courses require verified repair or explicit migration before another usable claim is promised. Migration preserves invitation/code associations; it never silently replaces an active classroom.

---

## Access Code Storage

A requested course's claim code is the exception: it is not in join rules, for the reason in "Claiming a course" above. Everything below describes the class code and client-set admin codes.

Access codes live in the `content.access_code` field of the room's `m.room.join_rules` state event. This is a Pangea-custom extension — the Matrix spec does not define this field. The code is set client-side when a course admin creates or configures a course.

### Code Lookup

Every code lookup goes through [`get_rooms_with_access_code`](../../synapse_pangea_chat/room_code/get_rooms_with_access_code.py): `knock_with_code`, `preview_with_code`, and the collision check that every new code passes. Scanning every room's join rules on each call cost about 100–200 ms of database CPU per code, and that cost grew with the number of courses. It ran on Synapse's shared database, so a class entering codes at once slowed `/sync` and message sending for everyone (#163).

Each Synapse process instead keeps a [`CodeIndex`](../../synapse_pangea_chat/room_code/code_index.py) in memory. It maps each code to the rooms whose current join rules carry it, and records how far through Synapse's event stream it is complete. The index only suggests candidates; the room's current join rules decide.

- **Every hit is confirmed.** Before we act on a candidate, we re-read that room's current join rules and check the code. Whether the code is an admin code also comes from those rules. A rotated code, a used admin code or a deleted course is never accepted from a stale index.
- **Every miss is checked against the database.** If no candidate survives, the index reads the join-rules changes committed since its last position, then checks again. A course created a moment ago on another process resolves on the first try. The index takes the committed position before reading, so an event that commits late is read twice rather than skipped.
- **Misses that arrive together share one catch-up.** A classroom mistyping one code costs a couple of queries, not one per student.
- **The index loads on first use.** The first lookup after a process starts reads every code. There is no startup step and no fallback to the old scan. The checks above mean a stale index can slow a lookup but can't make its answer wrong, so a bug is handled by a revert.

**Known limits:** the catch-up only sees newer changes. A state reset that points a room back to an older join-rules event would go unseen until the process restarts, and a valid code could report "not found" until then. Federation is off, which makes this close to impossible, and every deploy restarts the process. An upgraded room copies its predecessor's join rules, so both rooms carry the same code. A lookup then returns only the room the index already holds. The client never upgrades rooms.

---

## Invite Mechanics

The server needs a real user with invite power to issue the invite (Synapse's `update_room_membership` requires a sender). [`get_inviter_user`](../../synapse_pangea_chat/room_code/get_inviter_user.py) finds a local joined administrator whose power level meets the room's invite threshold. It never promotes a remaining member to manufacture an inviter; an empty room or one without an eligible administrator requires explicit recovery. If no such user exists, the room counts as failed (`NoInviterAvailableError`) and follows the failed-room handling above — it is never reported as invited.

---

## Rate Limiting

Per-user in-memory rate limiting. Configurable via module config:

- `knock_with_code_requests_per_burst` (default: 10)
- `knock_with_code_burst_duration_seconds` (default: 60)

Returns HTTP 429 when exceeded.

---
