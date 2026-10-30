---
applyTo: "synapse_pangea_chat/lti/**"
description: "What the module decides for the Canvas connection (LTI 1.3): it is off unless configured, which schools may connect, how a launch from Canvas is checked and handed to the app, and what a roster import may do."
---

# Canvas tool (LTI)

The module is how a school's Canvas (or another LMS) connects to Pangea: a teacher links a Canvas course to a Pangea course, imports the class list as seat invitations, and students open Pangea from Canvas. The flows are in the org [lms-integration](../../../.github/.github/instructions/lms-integration.instructions.md) doc; the invitations an import creates are in the org [seats](../../../.github/.github/instructions/seats.instructions.md) doc.

Canvas is not released: the tool runs only when the module is configured for it, no environment is, and admin-dash shows nothing Canvas.

## The experience

1. A school's Canvas admin installs Pangea; a Pangea server admin approves the school before anything works.
2. A teacher launches Pangea from Canvas and links the Canvas course to one of their Pangea courses.
3. The teacher imports the class list; each student becomes an invitation, with no email sent.
4. A student launches Pangea from Canvas and lands in the course, signing in or linking their account the first time.

## Decisions

### Switching it on

- Without its signing key in the module's config the tool does not exist, and the rest of the module runs normally. Re-decide: engineer.
- A school can only install by registering itself, and stays switched off until a Pangea server admin approves it. Re-decide: Satvik.
- A school's details must come from its own Canvas address, so one school cannot pose as another. Re-decide: engineer.

### Launches

- Every launch from Canvas is checked against the LTI 1.3 security rules, works once, and only in the browser that started it. Re-decide: engineer.
- Addresses a school's Canvas gives us are treated as untrusted, so the tool can never be used to reach Pangea's internal systems. Re-decide: engineer.
- A launch hands the app or admin-dash a short-lived one-time pass kept out of every server log; a student already linked may get a one-time sign-in instead. Re-decide: engineer.
- A server admin's account is never signed in from Canvas and is never managed, because a school's say-so must never open an admin account. Re-decide: Satvik.

### Linking and import

- Linking a Canvas identity to an account needs that account's own sign-in, and one Canvas identity links to one account per school. Re-decide: Satvik.
- A Canvas email is never treated as verified and never signs anyone in; an import uses it only to create an invitation. Re-decide: Satvik.
- A Canvas course links to one Pangea course, only by a teacher of that course, re-checked when linking. Re-decide: Satvik.
- An import checks the teacher first, reads the class list once, brings in only active students, never changes an existing invitation's email, and sends no email. Re-decide: Satvik.

### Privacy

- No pass, key, Canvas id or email is ever logged or sent to error tracking. Re-decide: engineer.
