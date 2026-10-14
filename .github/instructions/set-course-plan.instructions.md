---
applyTo: "synapse_pangea_chat/set_course_plan/**"
description: "Operator endpoint that points an existing course space at a different quest, gives it the new quest's Can-Do copy and renames it to match, without anyone joining, being promoted, or getting a notification."
---

# Set Course Plan

An endpoint only a server operator can call. It points an existing course space at a different quest, writes the course's own copy of the new quest's Can-Do list, and renames the space to match, so the space ends up as a course created from the new quest would be. It exists for catalog cleanup: before a duplicate quest can be deleted, every space that uses it has to move to the quest that is kept. A teacher can make this change themselves with the app's "Change course" button. This endpoint makes it for them.

It is the room half of the course plan replacement in [create-course-space](create-course-space.instructions.md) ("Replacing the course plan"): that design's rewrite of a claimed course's course plan, course copy, name and topic is this endpoint. It works by room, so it also covers spaces teachers made in the app, which have no invitation.

## Contract

- `POST /_synapse/client/pangea/v1/set_course_plan`
- **Auth:** server admins only.
- **Input:** the space's room ID, the quest ID it should point at, the quest ID the operator expects it to point at now, the new quest's title and description, and an optional dry run.
- **Response:** the old and new quest IDs, the space's name and topic before the call, which of the course copy, course plan, name and topic changed, the course copy's old and new revision and whether it could be built, and the member the events were sent as.

## What it does

1. Reads the space's current `pangea.course_plan`. A room with no course plan is not a course and is refused.
2. Compares the current plan ID with the expected one, reading it the way [public-courses](public-courses.instructions.md) does. If they differ, it refuses: the teacher may have changed course since the operator looked, and the teacher's choice wins.
3. If the course plan, name and topic already match and the space doesn't hold an empty course copy, it reports "unchanged" and writes nothing.
4. When the plan changes, it first writes the course's own copy (`pangea.course_missions`, see [content-studio](../../../.github/.github/instructions/content-studio.instructions.md)) for the new quest, so the space holds what a course created from that quest holds. Without this, a course that has a copy keeps listing the old quest's Can-Dos, because the app and the dashboard read the copy rather than the quest. The copy is built by the same CMS read a course gets at creation ([create-course-space](create-course-space.instructions.md), step 5), at a revision one above the old copy's (1 if there was none), so an open Studio refuses a stale publish. Like the copy made at creation, it holds the quest's Can-Dos in order with their library activities; the quest owner's private activities and the space's pins are not in it. After the read, it checks the course plan and copy again and refuses if either changed. If the read fails, the copy is written with empty content, so readers fall back to the new shared quest, and the failure is logged, as create-course-space requires. Sending the request again with the new quest as the expected one rebuilds the empty copy.
5. Then writes the course plan again with `uuid` set to the new quest ID. `l2` and every other field stay as they are, and the request has no way to change them, because a course's language doesn't change over its life. A legacy `course_plan_id` field is dropped, since everything now reads `uuid` first.
6. Then sets the space's name to the quest's title and its topic to the quest's description, each only when it differs. A name the teacher chose is replaced too; to keep it, the operator passes it back as the title, and the dry run reports the current name and topic for that.
7. Sends every event as the space's strongest local instructor, at power level 100, chosen by [`joined_local_instructors`](../../synapse_pangea_chat/room_code/instructor_access.py). In practice that's the teacher. Readers ignore a course copy sent below power level 100, so a weaker member is not used, and a space with no local instructor is refused and left untouched. Nobody's power level is raised and nobody joins. This is what create-course-space means by the module doing the rewrite "as the server": the write happens inside the module, so no instructor token or bot membership is needed, and the room's own instructor is the sender, the same way provisioning sends its later events.
8. If the room refuses a write (the instructor left or lost power in the meantime), the response says what had already landed and the error is logged. The copy goes first, so a refusal does not leave the new plan beside the old quest's Can-Dos. If the run stopped before the plan landed, sending the same request again finishes it. If it stopped after, sending it again with the new quest as the expected one writes the name and topic that still differ. Once the plan points at the new quest, a copy with content is not touched again, so a re-run does not wipe edits the teacher has published since; only an empty copy is rebuilt.
9. Logs the operator, the space, the old and new quest IDs, and what changed. The room state shows the teacher as the sender, so the log is how the change is traced back to the operator who made it.

A dry run does everything except the writes, including the CMS read, and reports what would happen.

## What the teacher sees

The space's new name and description in the app. The course plan change itself shows nowhere: the client doesn't show `pangea.course_plan` events in the timeline, and nothing sends a notification. The next time the teacher opens the course page, it shows the new quest. In the dashboard's Content Studio the course shows the new quest's list; the old copy's notes, due dates, order and removals are gone. Stars carry over only for activities under the new quest's Missions. Teacher pins are keyed by Mission ID, so they stop applying to Missions the new quest doesn't share (see the client's [quests](../../../client/.github/instructions/quests.instructions.md) design).

## Not in scope

- **Checking the quest.** The module's one CMS read is the course copy's. Confirming that the new quest exists, matches the space's language, and has the title and description passed in is the caller's job.
- **Unclaimed invitations.** Replacing an unclaimed invitation's quest is the invitation side of [create-course-space](create-course-space.instructions.md)'s replacement and isn't built here.
- **Batches.** One space per call, and the operator script loops over spaces.
