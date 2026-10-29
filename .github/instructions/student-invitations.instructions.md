---
applyTo: "synapse_pangea_chat/student_invitations/**,synapse_pangea_chat/email_invite/claim_by_email.py"
description: "What the module decides for seat invitations: one invitation per course and address, the invite email, how a student claims it, requests from another address, the managed-account record and the history."
---

# Student invitations

A teacher adds a student's email to a course; the module keeps that invitation, emails the student, and joins them to the course when they claim it; the student then gets a seat if the teacher has one for them. The flows and rules are in the org [seats](../../../.github/.github/instructions/seats.instructions.md) doc; seats themselves live in the choreographer, which asks the module which invitations a student has claimed. The [earlier course invites](invite-by-email.instructions.md) are separate and left as they are.

## The experience

1. The teacher adds an email; the module records the invitation and sends the invite email.
2. The student opens the link. Signed in with the invited email verified, they join the course. Signed in with another address, a request waits for the teacher.
3. The teacher grants or denies the request; a grant joins the student.
4. The Seats tab's Activity shows every step.

## Decisions

### Invitations and the email

- One invitation per course and address; a re-typed address with different capitals or spaces is the same one. Re-decide: Satvik.
- Adding an address that is already invited changes nothing; adding one that was revoked or left starts a fresh invitation. Re-decide: Satvik.
- The invite link is the course's class link plus the invitation, so the class code still joins the course without a seat. Re-decide: Satvik.
- A double click or a second tab sends one email, and a batch stops when the mail service itself fails, because the rest would fail the same way. Re-decide: engineer.
- The email's Canvas line stays off until Canvas is released. Re-decide: Satvik.

### Claiming

- Every way in (opening the link, the teacher's Grant or Approve all, signing in, or verifying a new address) goes through one claim, so two people can never claim the same invitation. Re-decide: engineer.
- Only the module matches an email to an account; no other service does. Re-decide: Satvik.
- Opening an invitation without the invited address verified records a request, and only the course's teachers see the address that account signed up with. Re-decide: Satvik.
- An account already joined in the course that opens another of its invitations is refused and nothing is recorded, because a person is their account, not the link they opened. Re-decide: Satvik.
- For a joined student with no invitation, the teacher can invite them to a seat from the student's own address without sending an email; the teacher never enters or sees the address in this action. Re-decide: Satvik.
- Leaving, being removed or being banned ends the invitation, which ends its seat. Re-decide: Satvik.

### Managed accounts

- Each claim marks the student as managed by the course, except a teacher of that course or a server admin, and the mark follows changes to the course's teachers. The module keeps each management record: the account, the course and when it began. Re-decide: Satvik.

### History and privacy

- Every invitation change goes into a history that is only ever added to; a sent email is recorded once the mail service accepts it. Re-decide: engineer.
- Teacher actions re-check that the caller teaches the course and refuse every other caller the same way, so a refusal reveals nothing about which courses exist. Re-decide: engineer.
- The sign-up hint shows only the course name and a masked address, only for an invitation still waiting. Re-decide: designer.
- No email or request content is ever logged or sent to error tracking. Re-decide: engineer.

## Open questions

- The org seats doc says management continues after a student leaves, until the teacher graduates them; today the managed mark ends when the student leaves. Graduation, and recording how management began and who graduated the student, are not built yet (pangeachat/.github#799). Owner: Satvik.
