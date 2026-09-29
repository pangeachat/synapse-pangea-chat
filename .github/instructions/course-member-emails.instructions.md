---
applyTo: "synapse_pangea_chat/course_member_emails/**,tests/test_course_member_emails_e2e.py,tests/test_course_member_emails_unit.py"
description: "A course admin reads the sign-in email of each student in their course, for the teacher dashboard. Who may call it, whose emails it returns, and what it never does."
---

# Course Member Emails — Synapse Module

Teachers asked to see each student's email on the dashboard's Students page, so they can match a Pangea account to a person on their class list and reach a student outside the app. The owner decided (2026-09) that a course's teacher may see the sign-in email of the students in that course.

Nothing that existed allowed it. A Matrix client can read only its OWN email (`/account/3pid`); every other path to another user's address needs a server-admin token, which the dashboard must never hold. This endpoint is the narrow exception: one course, the caller's own token, only that course's students.

## Who may call it

**A course admin of that course**: power level 100 in the course space, or a creator in room versions where creators hold unlimited power. Moderators (50) and students are refused. The bar is the teacher level, not the moderator level `invite_by_email` accepts, because this reads personal data rather than sending an invitation.

The caller must be joined to the course, and the room must be a space. Every refusal (unknown room, not a member, not a space, not an admin) is the same 403 with the same body, so the endpoint cannot be used to learn which rooms exist.

Calls are rate limited per caller.

## Whose emails it returns

| Rule | Why |
| --- | --- |
| Current membership is `join` | A student who leaves the course is no longer the teacher's student; their email disappears on the next call. Invited, knocking, left and banned members are never listed. |
| Not the caller, not another course admin | Co-teachers are colleagues, not students. |
| Not a bot or service account | They have no person behind them. Same naming rule as the rest of the module (`bot`, `bot-*`, `*-bot`). |
| Local accounts only | The homeserver only stores addresses for its own users. |
| Bound (validated) addresses only | A bound address is one the student proved they control at sign-up. |

A member with no bound email is **omitted** from the response rather than listed with a null. The caller already has its own roster; it treats "not in the response" as "no email". When a student has bound more than one address, the first one bound is returned, so the answer is stable between calls.

## What it never does

- **Log an address.** Log lines carry the room, the caller and counts only.
- **Change anything.** Read-only; binding and unbinding stay with Synapse.
- **Answer about anyone outside the course,** or about a course the caller does not administer.

## Rollout

Ships dark: `course_member_emails_enabled` (default off). With the flag off the route does not exist (404). Turning it on is an owner decision per environment, made together with the dashboard's own flags.

## Open question

Whether the privacy notice must say that a course's teachers can see a student's sign-in email, and how this applies to students under 18, is not settled (pangeachat/security#143). Production enablement waits on that answer.
