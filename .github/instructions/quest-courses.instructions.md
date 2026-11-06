---
applyTo: "synapse_pangea_chat/quest_courses/**"
description: "The three operator reads that tell the CMS which course spaces and unclaimed invitations reference each quest."
---

# Quest courses (Synapse)

## Purpose

When reviewing and editing content in the CMS, it helps to know where that content is used, so that content editors don’t delete or change a quest that live courses depend on. The CMS keeps its own copy of which course spaces and unclaimed courses use each quest. This module provides the three reads the CMS uses to keep that copy up to date.

## How it works

### Who may call

Only a server admin may call these reads, because they read rooms the caller is not in, private courses included. The reads return room IDs, quest IDs, languages, names and counts. They never return a member's identity or an email address.

### Space changes, from a cursor

Knowing which quest each space uses means reading the `pangea.course_plan` state event of every room on the server. That is too slow to run often, so this read returns only the spaces whose course plan, name or members changed after the caller’s cursor, each with its current quest, language, name and number of joined members, and the cursor to send next time. A room nobody touched is not read again, however many courses there are. A space’s quest is read with the same field and state-key precedence as public-courses, but for every room, not only those in the public directory, and a plan with no quest ID counts as no quest.

The changes come from Synapse's own record of changes to room state, the same record its room statistics and user directory follow. The cursor is the point up to which Synapse has finished writing that record, never simply the newest change seen, because on a server with workers changes can finish writing out of order and a change behind the newest one would otherwise be skipped for good. Sending the same cursor again returns the same changes again, so a failed sync just repeats.

A space comes back with no quest when its plan is removed, and also when the last member on this server leaves it, which is how a deleted space arrives, since deleting a space makes everyone leave. Either way the caller drops it. Nobody on this server can rejoin a room the server has left.

A caller with no cursor, or whose cursor is older than the record Synapse still keeps, is told to start over: it takes the current cursor, reads every course space once, with the same details, and follows changes from then on. A cursor names the server it came from, and one sent to another server is refused, so a CMS pointed at the wrong Synapse cannot mistake that server's changes for its own.

### Name and members

Both reads give each space's name and number of joined members, and only for rooms with a current course plan: a name or membership change in any other room is not reported. The bot is not counted, because it is not a person in the course. A space is live when at least one member other than the bot is joined; the teacher counts.

### Unclaimed courses, read whole

An unclaimed course is one of the following:

- a course invitation with no space made yet that has not been revoked, waiting to be claimed or claimed and still being built; it is unclaimed until its room is recorded, and from then on the space read covers it;
- an older course created at once with its space, which the bot holds until a teacher claims it.

Additional-instructor invitations are never listed, because their course already exists. The third read returns every unclaimed course with its quest, language, title, when it was made, and, for one still being built, when its claim started. A claim that has been building for more than an hour has failed, and is returned as stuck so the review does not show it as waiting forever. There are few unclaimed courses, so this read has no cursor. A quest replaced on an invitation shows as the new quest on the next read.

While a claimed course is being built, its space can get its course plan a moment before the invitation records the room. For that moment the course can show as both a space and an unclaimed course, or as neither; the next sync corrects it.

### Not covered

Removing the content of a course plan event by redaction does not appear as a change. Course plans are not redacted in normal use.
