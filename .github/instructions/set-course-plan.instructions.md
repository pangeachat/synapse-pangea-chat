---
applyTo: "synapse_pangea_chat/set_course_plan/**"
description: "Operator endpoint that points an existing course space at a different quest and renames it to match, without anyone joining, being promoted, or getting a notification."
---

# Set Course Plan

An endpoint only a server operator can call. It points an existing course space at a different quest and renames the space to match. It exists for catalog cleanup: before a duplicate quest can be deleted, every space that uses it has to move to the quest that is kept. A teacher can make this change themselves with the app's "Change course" button. This endpoint makes it for them.

It is the room half of the course plan replacement in [create-course-space](create-course-space.instructions.md) ("Replacing the course plan"): that design's rewrite of a claimed course's course plan, name and topic is this endpoint. It works by room, so it also covers spaces teachers made in the app, which have no invitation.

## Contract

- `POST /_synapse/client/pangea/v1/set_course_plan`
- **Auth:** server admins only.
- **Input:** the space's room ID, the quest ID it should point at, the quest ID the operator expects it to point at now, the new quest's title and description, and an optional dry run.
- **Response:** the old and new quest IDs, the space's name and topic before the call, which of the course plan, name and topic changed, and the member the events were sent as.

## What it does

1. Reads the space's current `pangea.course_plan`. A room with no course plan is not a course and is refused.
2. Compares the current plan ID with the expected one, reading it the way [public-courses](public-courses.instructions.md) does. If they differ, it refuses: the teacher may have changed course since the operator looked, and the teacher's choice wins.
3. If the course plan, name and topic already match, it reports "unchanged" and writes nothing.
4. Writes the event again with `uuid` set to the new quest ID. `l2` and every other field stay as they are, and the request has no way to change them, because a course's language doesn't change over its life. A legacy `course_plan_id` field is dropped, since everything now reads `uuid` first. Then it sets the space's name to the quest's title and its topic to the quest's description, each only when it differs. A name the teacher chose is replaced too; to keep it, the operator passes it back as the title, and the dry run reports the current name and topic for that.
5. Sends the events as the space's own highest-powered local member who can already send every event it changes, chosen by [`select_state_sender`](../../synapse_pangea_chat/public_courses/select_state_sender.py). In practice that's the teacher. Nobody's power level is raised and nobody joins. This is what create-course-space means by the module doing the rewrite "as the server": the write happens inside the module, so no instructor token or bot membership is needed, and the room's own instructor is the sender, the same way provisioning sends its later events. A space with no eligible member is refused and left untouched. If the room refuses the course plan (the chosen member left or lost power in the meantime), nothing is written. If it refuses the name or topic after the course plan landed, the response says what landed and the error is logged; running again with the new quest as the expected one finishes the job.
6. Logs the operator, the space, the old and new quest IDs, and what changed. The room state shows the teacher as the sender, so the log is how the change is traced back to the operator who made it.

A dry run does everything except the write, and reports what would happen.

## What the teacher sees

The space's new name and description in the app. The course plan change itself shows nowhere: the client doesn't show `pangea.course_plan` events in the timeline, and nothing sends a notification. The next time the teacher opens the course page, it shows the new quest. Stars carry over only for activities under the new quest's Missions. Teacher pins are keyed by Mission ID, so they stop applying (see the client's [quests](../../../client/.github/instructions/quests.instructions.md) design).

## Not in scope

- **Checking the quest.** Synapse can't see quest content and must not be taught to (see [public-courses](public-courses.instructions.md)). Confirming that the new quest exists, matches the space's language, and has the title and description passed in is the caller's job.
- **Unclaimed invitations.** Replacing an unclaimed invitation's quest is the invitation side of [create-course-space](create-course-space.instructions.md)'s replacement and isn't built here.
- **Batches.** One space per call, and the operator script loops over spaces.
