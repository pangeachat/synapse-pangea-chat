---
applyTo: "synapse_pangea_chat/student_invitations/**,synapse_pangea_chat/email_invite/claim_by_email.py,synapse_pangea_chat/email_invite/invite_by_email.py,synapse_pangea_chat/email_invite/build_join_url.py,synapse_pangea_chat/email_invite/student_invitation_api.py,synapse_pangea_chat/email_invite/student_invitations.py"
description: "What the Synapse module does for student invitations: keeping each invitation, sending its email on time, the one claim, the managed-account record it keeps today, and the per-address join links it sent before seats."
---

# Student invitations

## Purpose

The module is where a student invitation lives: it keeps the invitation a teacher adds, emails the student, runs the one claim that joins them to the course, and records that the course manages them. The rules a teacher or student meets are the org [joining courses](../../../.github/.github/instructions/joining-courses.instructions.md) doc's; what management means is the org [managed accounts](../../../.github/.github/instructions/managed-accounts.instructions.md) doc's; seats are the choreographer's, which asks the module which invitations a student has claimed. This doc covers only what the module decides on its own.

## The experience

1. The teacher adds an email on the dashboard; the module records the invitation and sends the email now, or on the day the teacher picked.
2. The student opens the link. Signed in with the invited address verified, they are in the course; signed in with another address, a request waits for the teacher.
3. The teacher grants or denies the request; a grant joins the student.
4. Every step shows in the course's Activity list.

## Sending the email

- The email is the shared course invitation template from synapse-templates, filled from the course itself: its name, description and teacher, and the invitation's link.
- A batch stops when the mail service itself fails, because every remaining address would fail the same way; a full mailbox holds up only that student, retried hourly, until the three-day limit the org doc sets.
- A send scheduled for later runs on the module's own clock, with no teacher sign-in kept; each invitation is re-read just before it goes, so one withdrawn or claimed since is never emailed.
- A waiting invitation can be sent early or moved to another day and is never emailed twice, because the send is counted before the email goes.
- The line telling the student to open Pangea from Canvas appears only for a course connected to Canvas, and stays off everywhere until Canvas is released.

## The claim

- Every way in (opening the link, the teacher's grant or Approve all, signing in, or verifying a newly added address) goes through one claim, so two people can never claim the same invitation. The module is the one place an address is matched to an account.
- The sign-up hint shows the course name and a masked address only while the invitation is still waiting.
- A teacher's action re-checks that the caller is a current admin of the course and refuses every other caller the same way, so a refusal reveals nothing about which courses exist.
- The history is only ever added to; a sent email enters it once the mail service accepts it.

## The managed-account record

- Each claim marks the student as managed by the course, except a teacher of that course or a server admin, and the mark follows changes to the course's teachers: a student made a teacher loses it. The module keeps each record: the account, the course and when management began.

## Invitations sent before seats

Before seats, the dashboard's class-list upload sent a different invitation through this module, and it still does until the seats release reaches production. Each address got its own join link, carrying a code of its own that opens the course exactly as the class link does ([knock with code](knock-with-code.instructions.md#codes-share-kit-and-existing-courses)). The code is never spent, so a second open or a forwarded link still joins, and each join through it is recorded against the invitation as evidence of who used it, never as a gate. The address stays with the invitation and is never returned by a read, so the dashboard marks who joined by invitation rather than by address; a course's admins can read when each was sent and how often it was used, without addresses. There is no claim, approval or seat on this path. The conference hook that invited every attendee of one conference to a shared course used it too and serves nothing now. When the path is removed is the org doc's open question.

## Open questions

- The record the module keeps today ends when the student leaves the course, while the org managed-accounts doc says management lasts until the teacher graduates them; graduation, how management began and who graduated the student are not recorded yet ([pangeachat/.github#799](https://github.com/pangeachat/.github/issues/799)).
