---
applyTo: "synapse_pangea_chat/email_invite/**"
description: "How an educator who asked for a course gets it: the invitation the team prepares, the emails, the claim that creates the course, and the share kit that follows."
---

# Create Course Space

An educator who asks for a course gets an email saying it is ready, taps the link, and the course is theirs. This exists so a requested course reaches the right person in one tap, and so no course is created until someone claims it. When each email is sent and what it says belong to the [educator funnel](../../../engagement/.github/instructions/teacher-funnel.instructions.md#how-a-course-reaches-them).

## What the educator sees

1. **The team prepares the course.** Once the quest is ready, an operator prepares an invitation: the quest, the course's name, language and cover, the educator's request in their own words, and the address they asked from (`requested_email`).
2. **An email says the course is ready.** Its one button leads to sign-up or sign-in and then to the course. It carries no class code. Reminders follow until the course is claimed, each with a fresh link.
3. **The educator claims it.** Whoever taps the link first becomes the course's admin, whatever account they use. An educator who signs in with `requested_email` gets the course without the link. The course is created at this moment ([claiming](knock-with-code.instructions.md#claiming-a-course)).
4. **The course is ready to teach.** It is a private course on the quest, in its language, with its cover. Its Can-Do list starts from the quest's Can-Dos, notes and selected activities, and the educator can change it from there ([course spaces](../../../.github/.github/instructions/course-spaces.instructions.md), [content lifecycle](../../../.github/.github/instructions/content-lifecycle.instructions.md#course-spaces)).
5. **The share kit arrives.** A second email brings the class link, the class code and a QR code. **Intended — not yet implemented:** the share kit opens the course on the educator dashboard, where the educator adds students by email, reviews the safety features, edits the curriculum and buys seats ([seats surface](../../../admin-dash/.github/instructions/seats-surface.instructions.md)); the class code is on the dashboard as a second way in.

## Who writes and sends each email

The engagement runner writes the first email, the reminders and the share kit, so a copy change needs no module release. Synapse sends them, because only Synapse can make a claim link, and it fills in the share kit's links (`destination`, `secondary_destination`) when the course is claimed. When no share kit is stored with the invitation, Synapse sends its own template.

- **Which address.** `requested_email` is the address the course is claimed by. A reminder may set `send_to`, for example when an educator asks us to write to a different address; that reminder, later mail and the share kit then go there.
- **Unsubscribe.** The first email answers the educator's own request and carries no unsubscribe link. Each reminder carries one, written by the runner as `unsubscribe_url` (Will, 2026-11-24).
- **Timing and the record.** The runner decides when to send, honours opt-outs and writes the Notification Log ([notices](../../../engagement/.github/instructions/notices.instructions.md)).

## Managing an invitation

Server administrators prepare, remind, inspect, revoke and replace invitations.

- **Preparing twice is safe.** The same request returns the same invitation and sends nothing again. A changed request under the same key is refused as a conflict.
- **Status.** A status read shows progress, who claimed, the course, replacements and delivery attempts. It leaves out addresses and codes. "Accepted" means the mail server took the message, which is short of reaching the inbox; an uncertain attempt is checked before anything is sent again.
- **Reminders** go to an invitation that is still waiting to be claimed. One that was partly claimed needs recovery first. A course made before invitations existed keeps its claim but cannot be reminded.
- **Revoking** stops further claims and leaves existing members where they are. Stopping outreach leaves the invitation claimable.
- **Replacing the course plan.** An operator can swap a starter quest for a tailored one in the same invitation, and its links and codes keep working. After the claim, [set-course-plan](set-course-plan.instructions.md) governs the change to the course itself.

## Why the course is created at the claim

An unclaimed course then never depends on the bot staying in a room ([design decision](https://github.com/pangeachat/synapse-pangea-chat/issues/231)). The requesting address and the claim codes stay out of room state, which students can read.

## Open questions

- Should creating the course require a readable course copy, and should the activity fallback use the quest's short ranked selection? Today a missing copy is allowed and the available library list is used.
- Does the CMS webhook proposed earlier still belong in future work?
