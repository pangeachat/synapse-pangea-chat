---
applyTo: "synapse_pangea_chat/student_invitations/**,synapse_pangea_chat/email_invite/claim_by_email.py"
description: "The module's student invitations for seats: one row per course and address, the invite email, the claim, requests from another address, the managed-account record and the invitation history."
---

# Student invitations

The module owns the invitation side of the org [seats](../../../.github/.github/instructions/seats.instructions.md) doc: the rows a teacher adds, the email, the claim that joins a student to the course, the requests a teacher grants or denies, and the managed-account record. That doc owns the flows, states and rules, including who may claim and what consent covers; this one owns what the module decides in carrying them out. Seats themselves live in the choreographer, which asks the module which invitations a student has claimed. The earlier course invitations of [invite_by_email](invite-by-email.instructions.md) are a separate store and are left as they are.

## Decisions

- **One row per course and address.** Addresses are compared case- and spacing-blind, so a re-typed address is the same row. Adding a live address changes nothing; adding one that was revoked or left starts a fresh invitation with earlier decisions cleared.
- **The invite email is the class link plus the invitation.** The class code still joins the course on its own; the invitation only adds the claim. A double click or a second tab sends one email, and a send stops when the mail service itself fails, because the rest would fail the same way. The email's Canvas line stays off until Canvas is released.
- **Invite to a seat.** For a joined student with no invitation, a row is made from the student's own address with no email sent. Neither the teacher nor the caller ever sees that address.
- **One claim path for every way in**: the student opening the invitation, the teacher's Grant and Approve all, sign-in or a newly verified address, and the Canvas link step. Two claims of one invitation cannot both win, a joined account cannot claim a second invitation in the same course, and no other service matches an address to an account.
- **A request is recorded only for a different address.** Opening an invitation without the invited address verified records a request, and only course admins see the address that account signed up with. An account already joined in the course that opens another invitation there is refused with a conflict and nothing is recorded, because identity is the account, not the link.
- **Managed exactly while not an admin.** Each claim records the managed account unless the claimant is an admin of that course or a server admin. The record follows the course's admins as they change, and each sign-in re-applies the rule to repair a missed change.
- **Leaving ends the invitation.** Leaving, being kicked or being banned sets the invitation Left, which ends its seat at the choreographer's next check.
- **One history.** Every invitation change is recorded in an append-only history that admin-dash-api reads for the Seats tab's Activity. A sent email is recorded after the mail service accepts it; if that record fails it is reported, and the send still counts.
- **Privacy.** Teacher routes re-check course admin on every request and answer every refusal the same way, so a route says nothing about which rooms exist. The public sign-up hint gives only the course name and a masked address, only for an invitation still Invited, and is rate limited. No address, request body or exception message is ever logged or sent to Sentry.
