---
applyTo: "synapse_pangea_chat/grant_instructor_analytics_access/**,synapse_pangea_chat/__init__.py,tests/test_grant_instructor_analytics_access_e2e.py"
---

# Grant Instructor Analytics Access — Synapse Module

Server-side endpoint that admin-force-joins a course's instructors into a student's analytics room, gated on the course having **Require analytics access to join** enabled. Triggered by the student's client when they join a toggle-on course or create a new analytics room inside one.

Cross-repo design: [course-analytics-access.instructions.md](../../../.github/.github/instructions/course-analytics-access.instructions.md).

## Endpoint

`POST /_synapse/client/pangea/v1/grant_instructor_analytics_access`

Lives in the `grant_instructor_analytics_access/` sub-package.

## Contract

- **Auth**: Matrix access token of the student (caller).
- **Request**: `{ "course_id": "!course:example.com", "room_id": "!analytics:example.com" }`
- **Validation**:
  - `course_id` and `room_id` must be valid Matrix room IDs.
  - Caller must be a joined member of `course_id` (403 otherwise).
  - Course's `pangea.course_settings` state event (state_key `""`) must have `require_analytics_access: true` (403 otherwise).
  - Target room's `m.room.create` content must have `type: "p.analytics"` (403 otherwise).
  - Caller must be the analytics room creator — the `m.room.create` sender (403 otherwise).

## Behavior

- Reads the course room's joined-member list, computes effective power level for each (creator → 100 when MSC4289 is on, else `users[user_id]`, else `users_default`), and selects all local non-bot members whose effective power level is **strictly greater** than the caller's. Among those, only the highest-power-level cohort is granted.
- For each instructor in the resulting set, performs an admin force-join into the analytics room. If the instructor is already joined, the action is `already_joined` and no event is generated. Otherwise the caller (who is the analytics room creator and therefore has invite power) is used as the inviter, then the instructor's own user is used as the sender of the join event.
- Bot accounts are filtered by user ID pattern: `@bot:*`, `@bot-*:*`, `@*-bot:*`.
- Federated target users are not supported — `is_mine` filters out non-local users from the candidate set.
- Partial success is part of the contract. One instructor's failure does not prevent attempts for the remaining instructors.

## Per-Instructor Results

- `instructors_joined`: list of `{ user_id, action }` where `action` is one of `joined`, `already_joined`.
- `errors`: list of `{ user_id, error }` for instructors whose join attempt raised.

A course with no candidate instructors (e.g., the caller is the highest-PL human) returns an empty `instructors_joined` and empty `errors` — not an error condition.

## Non-Goals

- Discovering analytics rooms server-side. The client passes the `room_id` it just created or detected; the server validates and grants.
- Language matching. The client decides which analytics room corresponds to the course's target language.
- Server-side toggle CRUD. The client writes the `pangea.course_settings` state event directly with normal room-power-level checks.
- Retroactive grants for students who joined before the toggle was flipped on. The pangea-bot operator script `run_grant_instructor_analytics_access.py` remains the manual escape hatch for that case.

## Future Work

_Last updated: 2026-04-30_

- Client integration: [pangeachat/client#6065](https://github.com/pangeachat/client/issues/6065)
