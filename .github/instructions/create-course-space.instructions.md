---
applyTo: "synapse_pangea_chat/email_invite/**"
description: "Preparing requested courses, delivering claim emails, and creating the educator's course when claimed."
---

# Create Course Space

A teacher who asks for a course gets an email saying it is ready, taps the link, and the course is theirs. Until that tap the course is only an invitation; the course itself is created when the teacher claims it. The [teacher funnel](../../../engagement/.github/instructions/teacher-funnel.instructions.md#how-a-course-reaches-them) owns fulfillment and outreach.

## The experience

1. **The team prepares the course.** Once the teacher's quest is ready, an operator prepares an invitation for it: the quest, the course's name, language and cover, the teacher's request in their own words, and the address they asked from.
2. **The teacher gets an email.** It says their course is ready and has one button, which leads to signing up or signing in and then to the course. It carries no class code. If they don't claim it, reminders follow, each with a fresh link.
3. **The teacher claims it.** Whoever taps the link first becomes the course's admin, whatever account they use. A teacher who installs the app first and signs in with the address the course was requested from gets the course without the link. The course is created at this moment, with them as its first admin ([claiming](knock-with-code.instructions.md#claiming-a-course)).
4. **The course is ready to teach.** It is a private course on the quest, in its language, with its cover. Its Can-Do list starts from the quest's Can-Dos, notes and selected activities, and the teacher can change it from there ([course spaces](../../../.github/.github/instructions/course-spaces.instructions.md), [content lifecycle](../../../.github/.github/instructions/content-lifecycle.instructions.md#course-spaces)). Students join with the class code and share their analytics with the teacher.
5. **The teacher gets the share kit.** A second email, to the address the course was requested from, brings the class link, the class code and a QR code to put on a slide ([share kit](knock-with-code.instructions.md#codes-share-kit-and-existing-courses)). **Intended — not yet implemented:** the email sends the teacher to the course's roster on the dashboard to invite their students by email ([seats surface](../../../admin-dash/.github/instructions/seats-surface.instructions.md)), which we now recommend over sharing the class link or code.

## Logic and tradeoffs

Creating at claim means an unclaimed course never depends on the bot remaining in a room ([design decision](https://github.com/pangeachat/synapse-pangea-chat/issues/231)). The requesting address and claim codes never enter room state, which students can read.

The runner writes the first email, the reminders and the share kit, so a copy change needs no module deploy. Synapse only puts the claim link in, because only Synapse may mint one; the runner's latest stored share kit is sent at the claim, or the module's own template when there is none. A caller that writes no first email can have preparation send the module's template. A course records its quest and language the way the [public catalog](public-courses.instructions.md) reads them.

The request key identifies one course request within the preparing operator's account. Unchanged input returns the same invitation and current status without resending; changed input conflicts. The invitation is persisted before email. Status reads have no delivery or provisioning side effects and expose progress, the claimant and room when known, replacements and delivery attempts, never addresses or raw codes. Outcomes distinguish unsent, accepted, failed and uncertain; acceptance is not inbox delivery. Reconcile uncertain attempts before resending.

Only server administrators prepare, remind, inspect, revoke or replace invitations. Reminders mint another claim link for the same invitation and requesting address. Missing, revoked, completed or partially claimed invitations cannot receive reminders; partial claims need recovery. Revocation prevents further claims without removing membership. Stopping outreach does not revoke an invitation. The runner owns timing, refusals and Notification_Log records under [notices](../../../engagement/.github/instructions/notices.instructions.md); claim mail keeps its [dedicated delivery path](notice-delivery.instructions.md#deliver-a-notice).

### Replacing the course plan

An operator can replace a starter quest with a tailored quest in the same invitation, checking the expected current quest to avoid overwriting another change. Dry runs write nothing. Before claiming, replacement updates the preparation and preserves its codes; during provisioning it is refused. After completion, [set-course-plan](set-course-plan.instructions.md) governs the room rewrite. Membership, power levels, codes and the share kit stay unchanged. Revoked and additional-instructor invitations cannot be replaced this way.

## Existing courses and open questions

Legacy room-backed courses retain their claims through knock_with_code and status reads by room ID, but cannot receive reminders.

Should creation require a readable course copy, and should activity fallback use the quest's short ranked selection? Current provisioning permits a missing copy and uses the available library list. Does the previously proposed CMS webhook still belong in future work?
