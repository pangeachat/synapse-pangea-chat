---
applyTo: "synapse_pangea_chat/set_course_plan/**"
description: "Operator endpoint that points an existing course space at a different quest without anyone joining, being promoted, or seeing a notice."
---

# Set Course Plan

An endpoint only a server operator can call. It points an existing course space at a different quest. It exists for catalog cleanup: before a duplicate quest can be deleted, every space that uses it has to move to the quest that is kept. A teacher can make this change themselves with the app's "Change course" button. This endpoint makes it for them without their noticing.

## Contract

- `POST /_synapse/client/pangea/v1/set_course_plan`
- **Auth:** server admins only.
- **Input:** the space's room ID, the quest ID it should point at, the quest ID the operator expects it to point at now, and an optional dry run.
- **Response:** the old and new quest IDs, the member the event was sent as, and whether anything changed.

## What it does

1. Reads the space's current `pangea.course_plan`. A room with no course plan is not a course and is refused.
2. Compares the current plan ID with the expected one, reading it the way [public-courses](public-courses.instructions.md) does. If they differ, it refuses: the teacher may have changed course since the operator looked, and the teacher's choice wins.
3. If the space already points at the new quest, it reports "unchanged" and writes nothing.
4. Writes the event again with `uuid` set to the new quest ID. `l2` and every other field stay as they are, and the request has no way to change them, because a course's language doesn't change over its life. A legacy `course_plan_id` field is dropped, since everything now reads `uuid` first.
5. Sends the event as the space's own highest-powered local member who can already send it, chosen by [`select_state_sender`](../../synapse_pangea_chat/public_courses/select_state_sender.py). In practice that's the teacher. Nobody's power level is raised and nobody joins. A space with no eligible member is refused and left untouched.
6. Logs the operator, the space, and the old and new quest IDs. The room state shows the teacher as the sender, so the log is how the change is traced back to the operator who made it.

A dry run does everything except the write, and reports what would happen.

## What the teacher sees

Nothing at the time. The client doesn't show `pangea.course_plan` events in the timeline, and the change sends no notification. The next time the teacher opens the course page, it shows the new quest. Stars carry over only for activities under the new quest's Missions. Teacher pins are keyed by Mission ID, so they stop applying (see the client's [quests](../../../client/.github/instructions/quests.instructions.md) design).

## Not in scope

- **Checking the quest.** Synapse can't see quest content and must not be taught to (see [public-courses](public-courses.instructions.md)). Confirming that the new quest exists and matches the space's language is the caller's job.
- **Unclaimed invitations.** A prepared invitation's quest can't change (see [create-course-space](create-course-space.instructions.md)). Its space is moved after it's claimed.
- **Batches.** One space per call, and the operator script loops over spaces.
