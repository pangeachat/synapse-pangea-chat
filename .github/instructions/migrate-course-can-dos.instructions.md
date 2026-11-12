---
applyTo: "synapse_pangea_chat/migrate_course_can_dos/**,scripts/migrate_course_can_dos.py"
description: "One-off operator endpoint and script that move each course space from old (pre-merge) Can-Do ids to the merged Can-Dos, keeping every teacher edit."
---

# Migrate Course Can-Dos

Step 8 of the Can-Do migration ([pangeachat/.github#832](https://github.com/pangeachat/.github/issues/832)): merged Can-Dos replace the old ones, one per family. A course space names Can-Dos in its Can-Do list (`pangea.course_missions`, see [content-studio](../../../admin-dash/.github/instructions/content-studio.instructions.md)) and in its pins (`pinned_activities_by_objective` in `pangea.teacher_mode`). This operation moves both to the merged Can-Dos. It runs on each environment right after that environment's plans are migrated, and is removed after the last progress pass. A course with neither a Can-Do list of its own nor pins reads its plan, which is already moved.

## Contract

- `POST /_synapse/client/pangea/v1/migrate_course_can_dos`, server admins only, one space per call.
- **Input:** the room ID; the environment's map of old Can-Do row id to merged row id (row ids differ between environments; a merged id is never itself mapped); and either `dry_run: true` or `expected`, the ids of the list and pins events the dry run read.
- **Response:** what would change: entries combined, slots given up, notes trimmed with the text dropped, pin sets pooled, the events to send and their sender.
- **Refused (409):** no local instructor; a write whose space changed since its dry run; a write the room refuses.

## What it does

1. Rewrites the list as raw JSON, so every field it does not change stays, Content Studio's included (`start_date`, `holidays`, `city`, `deleted_activity_ids`, later ones).
2. Replaces each old id with its merged id in `missions` and `removed_missions`. A Can-Do appears once per list, so old Can-Dos in one course that map to one merged Can-Do combine (Gabby, 2026-11-11):
   - The combined entry takes the first occurrence's place. Occurrences next to it merge into one span with the last one's `due_at`, or the latest earlier date in the span when the last has none. A later occurrence with other entries between gives up its slot and its date.
   - `activity_ids` keeps the first entry's order, then adds the others' new ids; `removed_activity_ids` and `deleted_activity_ids` pool the same way. An activity one entry shows and another hid stays shown, as readers already show it.
   - Distinct notes join with " / ". Over 280 UTF-16 units, as the app counts, the first note is kept with "(+N more)", cut to fit if it is long itself, and the dropped text is logged.
   - `city` comes from the first entry that has one; fields this module does not know come from the first entry.
   - Removed Can-Dos combine the same way, what each showed at removal first. When the course still shows the merged Can-Do, a removed old one's activities join that entry as hidden, still credited.
3. Bumps `revision`, so Studio refuses a publish from a draft opened before the migration.
4. Re-keys the pins and keeps every other `pangea.teacher_mode` field, `xp_to_complete_objective` included. Pins that land on one Can-Do pool. A Can-Do that had no pins adds nothing, so the merged Can-Do shows only the pinned activities.
5. Leaves alone, and reports, a list readers already ignore: one that fails the app's strict parse, or whose sender is no longer at power level 100. Pins are re-keyed whoever sent them, since the app reads them from any sender.
6. Sends the list, then the pins, as ordinary state events from the space's strongest local instructor, as [set-course-plan](set-course-plan.instructions.md) does, so readers accept the list and the [analytics forwarder](analytics-forward.instructions.md) updates the dashboard's mirror. Nobody joins and no power level changes.
7. Writes only while the space still holds both events its dry run read. A teacher's edit always makes a new event, so an edited space is skipped and reported, never overwritten. A re-run reads current state, so it never undoes an edit, and a space with no old id left has nothing to write. The check and the send are not atomic: a publish in the moment between them is lost.

## The operator script

`scripts/migrate_course_can_dos.py` dry-runs every space on the homeserver, or the ones named, and writes a report to read. `--apply` then writes only the spaces the report says would change, each with its dry run's `expected`, and refuses a report made on another homeserver. Reports quote teachers' notes: keep them out of the repo.

## Not in scope

- **Activity copies** (activity rows with `course_room_id`). Production has none, and staging's were deleted (#832). An activity copy made before the switch names only old Can-Dos, and the course listing finds a course's activity copies by Can-Do, so it drops out of its course's view; how the migration treats such copies is open on #832.
- **The dashboard's stores** (caches, stored first-completion dates, open drafts) move on its side, in the order the migration's deploy note gives.
- **Learners' progress** moves right after, in [migrate-course-progress](migrate-course-progress.instructions.md).
