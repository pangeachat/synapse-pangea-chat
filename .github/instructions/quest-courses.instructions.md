---
applyTo: "synapse_pangea_chat/quest_courses/**"
description: "Quest courses, Synapse side: the three operator reads that tell the CMS which course spaces and unclaimed courses use each quest, and why the space read follows a cursor instead of scanning every room."
---

# Quest courses (Synapse)

## Purpose

The CMS content review shows, for each quest, the course spaces built on it that people are in, and the courses for it that nobody has claimed yet. The CMS keeps its own copy of that list ([content review](../../../cms/.github/instructions/content-review.instructions.md#courses-using-a-quest)). These three reads are how it fills that copy without scanning every room on the server each time.

## The experience

A reviewer opens a quest in the content review and sees how many live spaces and unclaimed courses use it, and which ones. Nothing here is seen by a learner or a teacher.

## How it works

### Who may call

Only a server admin may call these reads, because they list every course on the server, private ones included. The CMS calls them as the bot account, which is already a server admin, using one stored session as [access controls](../../../.github/.github/instructions/access-controls.instructions.md#matrix-service-account-sessions) require rather than a login per call. The reads return room IDs, quest IDs, languages, names and counts. They never return a member's identity or an email address.

### Space changes, from a cursor

A space uses a quest when its current `pangea.course_plan` names that quest, read with the same field and state-key precedence as [public-courses](public-courses.instructions.md), but for every room, not only those in the public directory. A plan with no quest ID counts as no quest. Reading that for every room at once is too slow to run often, so this read returns only the spaces whose course plan changed after the caller's cursor, each with its current quest and language, and the cursor to send next time.

The changes come from Synapse's own record of changes to room state, the same record its room statistics and user directory follow. The cursor is the point up to which Synapse has finished writing that record, never simply the newest change seen, because on a server with workers changes can finish writing out of order and a change behind the newest one would otherwise be skipped for good. Sending the same cursor again returns the same changes again, so a failed sync just repeats.

A space comes back with no quest when its plan is removed, and also when the last member on this server leaves it, which is how a deleted space arrives, since deleting a space makes everyone leave. Either way the caller drops it. Nobody on this server can rejoin a room the server has left.

A caller with no cursor, or whose cursor is older than the record Synapse still keeps, is told to start over: it takes the current cursor, reads every course space once, and follows changes from then on.

### Name and members, for spaces the caller holds

Members join and leave without changing the course plan, so a second read takes a batch of room IDs and returns each room's name and number of joined members. The bot is not counted, because it is not a person in the course. A space is live when at least one member other than the bot is joined; the teacher counts.

### Unclaimed courses, read whole

An unclaimed course is one whose teacher has not claimed it yet:

- a course invitation with no space made yet and not revoked, waiting to be claimed or claimed and still being built; it is unclaimed until its room is recorded, and from then on the space read covers it;
- an older course created at once with its space, which the bot holds until a teacher claims it.

Additional-instructor invitations are never listed, because their course already exists. The third read returns every unclaimed course with its quest, language, title, when it was made, and, for one still being built, when its claim started. A claim that has been building for more than an hour has failed, and is returned as stuck so the review does not show it as waiting for ever. There are few unclaimed courses, so this read has no cursor. A quest replaced on an invitation shows as the new quest on the next read.

While a claimed course is being built, its space can get its course plan a moment before the invitation records the room. For that moment the course can show as both a space and an unclaimed course, or as neither; the next sync corrects it.

### Not covered

Removing the content of a course plan event by redaction does not appear as a change. Course plans are not redacted in normal use.
