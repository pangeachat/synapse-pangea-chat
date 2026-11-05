---
applyTo: "synapse_pangea_chat/quest_courses/**"
description: "Quest courses, Synapse side: the three operator reads that tell the CMS which course spaces and unclaimed invitations use each quest, and why the space feed follows a cursor instead of scanning every room."
---

# Quest courses (Synapse)

## Purpose

The CMS content review shows, for each quest, the course spaces built on it that people are in, and the course invitations for it that have no space yet. The CMS keeps its own copy of that list ([content review](../../../cms/.github/instructions/content-review.instructions.md#courses-using-a-quest)). These three reads are how it fills that copy without scanning every room on the server each time.

## The experience

A reviewer opens a quest in the content review and sees how many live spaces and open invitations use it, and which ones. Nothing here is seen by a learner or a teacher.

## How it works

### Who may call

Only a server admin may call these reads, because they list every course on the server, private ones included. The CMS calls them with its own service account, kept as [access controls](../../../.github/.github/instructions/access-controls.instructions.md#matrix-service-account-sessions) require. The reads return room IDs, quest IDs, languages, names and counts. They never return a member's identity or an email address.

### Space changes, from a cursor

A space uses a quest when its current `pangea.course_plan` names that quest, read the way [public-courses](public-courses.instructions.md) reads it. Reading that for every room at once is too slow to run often. So this read returns only what changed: every space whose course plan was set, changed or removed after the caller's cursor, oldest first, with its quest and language, and the cursor to send next time. A removed plan is returned with no quest, so the caller drops the space.

The changes come from Synapse's own record of changes to room state, the same record Synapse's room statistics and user directory follow. A caller that sends the same cursor again gets the same changes again, so a failed sync just repeats.

A caller with no cursor yet asks for the current cursor first, then reads every course space once, page by page, and then follows changes from that cursor. A change that lands during that first read is returned twice, which does no harm.

### Name and members, for spaces the caller already holds

A deleted room leaves nothing in that record, and members join and leave all the time without changing the course plan. So a second read takes a batch of room IDs and returns each room's name and number of joined members, or says the room no longer exists. A space is live when at least one member is joined; the teacher counts.

### Open invitations, read whole

An open invitation is a course invitation with no space made yet and not revoked: one waiting to be claimed, or claimed and still being built. It is open until its room is recorded, and from then on the space read covers it. Additional-instructor invitations are never open, because their course already exists. The third read returns every open invitation with its quest, language, title and when it was made. There are few of them, so it has no cursor. A quest replaced on an invitation shows as the new quest on the next read.

While a claimed course is being built, its space can get its course plan a moment before the invitation records the room. For that moment the course can show as both, or as neither; the next sync corrects it.

## Open questions

- Whether the CMS's existing Matrix service account is made a server admin for these reads, or gets a separate admin account.
