---
applyTo: "synapse_pangea_chat/set_course_plan/**"
description: "Operator endpoint that points an existing course space at a different quest, gives it the new quest's Can-Do copy and renames it to match, without anyone joining, being promoted, or getting a notification."
---

# Set Course Plan

An endpoint only a server operator can call. It moves an existing course space to a different quest so the space ends up as a course created from that quest would be: the course's own copy of the quest's Can-Do list, the course plan, and the space's name and topic. It exists for catalog cleanup: before a duplicate quest can be deleted, every space that uses it has to move to the quest that is kept. It makes for the teacher the change the app's "Change course" button lets them make themselves. It is the room half of the course plan replacement in [create-course-space](create-course-space.instructions.md) ("Replacing the course plan"), and it works by room, so it also covers spaces teachers made in the app. The request, response and step-by-step mechanics are in the module's docstrings.

- **The teacher's choice wins.** The operator names the quest they expect the space to hold now. If the teacher changed course since the operator looked, or changed the plan or its copy while the endpoint was building the new one, the call is refused and nothing is written.
- **The course lists the new quest's Can-Dos.** The copy is rebuilt by the CMS read creation uses and written before the plan, so a part-way failure never leaves the new plan beside the old Can-Dos. A failed read writes an empty copy (readers fall back to the shared quest) and is logged. Once on the new quest, a copy with content is never rebuilt, so a re-run does not wipe the teacher's edits; an empty or missing copy is built ([admin-dash-api#472](https://github.com/pangeachat/admin-dash-api/issues/472)).
- **Nobody joins, is promoted or is notified.** Events are sent as the space's strongest local instructor at power level 100, because readers ignore a copy sent below that; a space with none is refused. The server log records the operator.
- **The name and topic follow the quest; the language never changes.** A teacher-chosen name is replaced unless the operator passes it back; a dry run reports the current name and topic. Checking the quest exists, matches the language, and matches the title and description is the caller's job.
- **What the teacher sees.** The new name and description, and the new quest on the course page and in Content Studio on next open; nothing in the timeline. The old copy's notes, due dates, order and removals are gone. Stars carry over only under the new quest's Can-Dos; pins stop applying to Can-Dos it does not share.
