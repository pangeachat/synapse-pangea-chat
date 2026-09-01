---
applyTo: "synapse_pangea_chat/grant_instructor_analytics_access/**,synapse_pangea_chat/__init__.py,tests/test_grant_instructor_analytics_access_e2e.py"
---

# Grant Instructor Analytics Access — Synapse Module

Server-side endpoint that admin-force-joins a course's instructors into a student's analytics room. Always called by the student, for their own analytics room, and always gated on evidence that the student agreed. There are two such gates, and a request uses exactly one of them:

| Gate | The evidence | Who is granted | Triggered by |
|------|--------------|----------------|--------------|
| **Course toggle** | The course has **Require analytics access to join** enabled, so joining it was the agreement | The course's instructor cohort, computed server-side | The student's client on joining a toggle-on course, or on creating a new analytics room inside one |
| **Consented request** | The named instructor is knocking on this analytics room, and the student answered the knock with Allow | The same cohort, plus the instructor who asked | The student's client when they allow a pending analytics request |

The consented-request gate exists because analytics is optional on most courses, and there a teacher's only route is to ask. Answering that request by inviting the teacher was never enough: an invited teacher is not a joined teacher, so every analytics read kept failing until they accepted, and a teacher who works from the admin dashboard never opens the app to accept at all.

Cross-repo design: [course-analytics-access.instructions.md](../../../.github/.github/instructions/course-analytics-access.instructions.md).

## Endpoint

`POST /_synapse/client/pangea/v1/grant_instructor_analytics_access`

Lives in the `grant_instructor_analytics_access/` sub-package.

## Contract

- **Auth**: Matrix access token of the student (caller).
- **Request**: `{ "mx_course_id": "!course:example.com", "mx_analytics_room_id": "!analytics:example.com" }`, both Matrix room IDs (the `mx_` prefix disambiguates them from CMS course-plan UUIDs). Adding `"mx_instructor_id": "@teacher:example.com"` — the instructor whose request the student just allowed — selects the consented-request gate; omitting it selects the course-toggle gate.
- **Validation**, in every request:
  - `mx_course_id` and `mx_analytics_room_id` must be valid Matrix room IDs, and `mx_instructor_id` a valid Matrix user ID (400 otherwise).
  - Caller must be a joined member of `mx_course_id` (403 otherwise).
  - `mx_analytics_room_id`'s `m.room.create` content must have `type: "p.analytics"` (403 otherwise).
  - Caller must be the analytics room creator — the `m.room.create` sender (403 otherwise).
- **Validation**, per gate:
  - Without `mx_instructor_id`: the course's `pangea.course_settings` state event (state_key `""`) must have `require_analytics_access: true` (403 otherwise).
  - With `mx_instructor_id`: that user's current membership in `mx_analytics_room_id` must be `knock` (403 otherwise). The course toggle is not read — an optional course is the case this gate is for. Denying a request kicks the knock, which drops the membership to `leave` — so a denied request can no longer be granted.

## Behavior

- The instructor cohort is the same under both gates: read the course room's joined-member list, compute effective power level for each (creator → 100 when MSC4289 is on, else `users[user_id]`, else `users_default`), keep all local non-bot members whose effective power level is **strictly greater** than the caller's, and among those keep only the highest-power-level tier.
- Under the consented-request gate the instructor who asked is added to that cohort. A course usually has more than one teacher and only one of them has to ask, so granting the asker alone would leave the co-teachers reading 403s exactly as before; and the asker may be a teaching assistant below the cohort's power level, so they have to be added rather than assumed present.
- For each instructor in the resulting set, performs an admin force-join into `mx_analytics_room_id`. If the instructor is already joined, the action is `already_joined` and no event is generated. Otherwise the caller (who is the analytics room creator and therefore has invite power) is used as the inviter, then the instructor's own user is used as the sender of the join event.
- Before inviting, the endpoint applies both halves of [analytics room push suppression](analytics-room-push-suppression.instructions.md) to each instructor, so the invite that precedes the join does not notify them.
- Bot accounts are filtered by user ID pattern: `@bot:*`, `@bot-*:*`, `@*-bot:*`.
- Federated instructors are not supported. Under the course-toggle gate `is_mine` filters them out of the candidate set; under the consented-request gate a named remote user is attempted and its failure lands in `errors`, since a remote homeserver's user cannot be force-joined from here.
- Partial success is part of the contract. One instructor's failure does not prevent attempts for the remaining instructors.

## Per-Instructor Results

- `instructors_joined`: list of `{ user_id, action }` where `action` is one of `joined`, `already_joined`.
- `errors`: list of `{ user_id, error }` for instructors whose join attempt raised.

A course with no candidate instructors (e.g., the caller is the highest-PL human) returns an empty `instructors_joined` and empty `errors` — not an error condition.

## Non-Goals

- Discovering analytics rooms server-side. The client passes the `mx_analytics_room_id` it just created or detected; the server validates and grants.
- Language matching. The client decides which analytics room corresponds to the course's target language.
- Server-side toggle CRUD. The client writes the `pangea.course_settings` state event directly with normal room-power-level checks.
- Retroactive grants for students who joined before the toggle was flipped on. The pangea-bot operator script `run_grant_instructor_analytics_access.py` remains the manual escape hatch for that case.

## Future Work

_Last updated: 2026-04-30_

- Client integration: [pangeachat/client#6065](https://github.com/pangeachat/client/issues/6065)
