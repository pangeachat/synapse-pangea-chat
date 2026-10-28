---
applyTo: "synapse_pangea_chat/email_invite/**"
---

# Invite by Email — Synapse Module

Cross-repo design: [conference-course-invite.instructions.md](../../../.github/.github/instructions/teacher-funnel.instructions.md)

## Endpoint

`POST /_synapse/client/pangea/v1/invite_by_email`

- **Auth**: Matrix access token. Caller must be admin (power level 100) in the room → 403 otherwise.
- **Request**: `{ room_id, emails, message? }`
- **Response**: `{ emailed, errors, invitations }`, where `invitations` is `[{ email, invitation_id }]`, one per address emailed.

## Behavior

For each email, send a branded Jinja2 email with a join link.

**One invitation per address, with its own code** (pangeachat/.github#718; Will, 2026-10-14). Each address gets a student invitation: a fresh seven-character code from the shared generator, free in join rules, among claim codes and among other student invitations, stored as a digest with the room, the address, the inviter and the time (`pangea_student_invitation`). The email's link is that code, `<app>/<code>` ([build_join_url](../../synapse_pangea_chat/email_invite/build_join_url.py)), so a join names the invitation it came from. The share kit and the QR keep the class code; a student who types a code still joins with the class code.

**The code is never spent.** It opens the course like the class link for as long as the class link does, so a student who opens the email twice, or a forwarded link, still joins; `knock_with_code` resolves it before the class-code lookup and records each join against the invitation, the first as the invitee's (`first_used_by`, `first_used_at_ms`) and every one in `uses`. Attribution is evidence, never a gate: nothing about the join depends on the code being the invitee's.

**The address stays with the invitation**, as a requested course's requesting address does, so a reminder to an invited student who has not joined can be sent by invitation id without the caller holding the address, and the dashboard marks who joined by id rather than guessing from an address. It is never returned by a read. Retention follows the dashboard's roster (admin-dash-api roster-invites); there is no purge yet.

**Reading a course's invitations**: `GET /_synapse/client/pangea/v1/student_invitations?room_id=` for the course's admins (power level 100) and server admins (the engagement runner): each invitation's id, when and by whom it was sent, first use and use count. No addresses.

Room name, description, avatar, access code, and inviter identity are all read from room state — nothing is passed in the request. Inviter identity = all human users at the highest power level in the room.

The optional `message` field is included in the email as a personal note from the inviter.

## Joining without the link

When an account's verified email matches a student invitation, the app offers the student that course, so a student who signed up alone still gets in.

1. The match runs at sign-in, when an email is verified, and when a teacher invites an address that already has an account. The email still goes out.
2. When the student next opens the app, it shows the course and the inviting teacher, with Join and Decline. Closing it unanswered brings it back next time.
3. Join works exactly as the link does, and the dashboard shows the student as joined.

- **An offer, not a join.** Reason: a server-made join skips the app's own join, where analytics sharing and the join notices happen. Re-decided by: Will.
- **Only the student sees the offer;** it is not a course invite. Reason: everyone in a course sees its invites, so one sent on a match would show the teacher and the class who owns the address before the student acts. Re-decided by: Will.
- **Verified addresses only,** ignoring case. Reason: a mistyped address must not pull someone into a stranger's class. Other addresses, including Apple's Hide My Email, use the link or the code. Re-decided by: Will.
- **Each invitation is offered to an account once.** A decline is final, and leaving the course does not bring the offer back. Reason: otherwise every sign-in would offer it again. Re-decided by: Will.
- **Only invitations from the last 120 days are offered,** one seat term. Reason: invitations never expire, and an old one must not pull a student into a past course. Re-decided by: Will.
- **No offer to an account in the course, banned from it, or blocked by every admin,** as with the course code. Re-decided by: engineer.

## Email

Uses Synapse's built-in email sending (SMTP via Exim → SES). Templates live in [synapse-templates](../../../synapse-templates/) — same deployment pattern as registration/password-reset emails. See [course-invite-by-email.instructions.md](../../../synapse-templates/.github/instructions/course-invite-by-email.instructions.md) for template design.

## Reused from `room_code/`

- Room invite helper
- Inviter resolution from power levels
- Auth pattern from `knock_with_code`

## Future Work

- Rate limiting / batch size limits — no issue yet
- A reminder to an invited student who has not joined, sent by invitation id with the caller's rendered copy, as the claim reminder is (pangeachat/.github#718)
- A purge of invitations whose roster rows are gone
