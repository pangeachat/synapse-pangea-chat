---
applyTo: "synapse_pangea_chat/migrate_course_can_dos/**,scripts/migrate_course_can_dos.py"
description: "One-off operator endpoint and script that move each course space from old (pre-merge) Can-Do ids to the merged Can-Dos: the exact rules for combining a live course's entries, what each rule leaves out, and how every outcome is reported."
---

# Migrate Course Can-Dos

Step 8 of the Can-Do migration ([pangeachat/.github#832](https://github.com/pangeachat/.github/issues/832)): merged Can-Dos replace the old ones, one per family. A course space names Can-Dos in its Can-Do list (`pangea.course_missions`, see [content-studio](../../../admin-dash/.github/instructions/content-studio.instructions.md)) and in its pins (`pinned_activities_by_objective` in `pangea.teacher_mode`). This operation moves both to the merged Can-Dos. It runs on each environment right after that environment's plans are migrated, and is removed after the last progress pass. A course with neither a Can-Do list of its own nor pins reads its plan, which is already moved. This doc runs past 800 words because its combining rules are the one written authority for a live course: the dashboard does not combine a live course's drafts itself ([admin-dash-api#494](https://github.com/pangeachat/admin-dash-api/pull/494)).

## Contract

- `POST /_synapse/client/pangea/v1/migrate_course_can_dos`, server admins only, one space per call.
- **Input:** the room ID; the environment's map of old Can-Do row id to merged row id (row ids differ between environments; a merged id is never itself mapped); and either `dry_run: true` or `expected`, the ids of the list and pins events the dry run read.
- **Response:** what would change, as ids and counts: entries combined, slots given up, and for each combined entry the due dates, cities and other fields it leaves out and the hidden activities it shows; notes trimmed, as lengths; pin sets pooled, old Can-Dos left unpinned, pin values dropped; how many old ids a list left alone still names; the events to send and their sender.
- **Refused (409):** no local instructor; a write whose space changed since its dry run; a write the room refuses.

## What it does

1. Rewrites the list as raw JSON, so every field it does not change stays, Content Studio's included (`start_date`, `holidays`, `city`, `deleted_activity_ids`, later ones).
2. Replaces each old id with its merged id in `missions` and `removed_missions`. A Can-Do appears once per list, so old Can-Dos in one course that map to one merged Can-Do combine (Gabby, 2026-11-11), by the rules below.
3. Bumps `revision`, so Studio refuses a publish from a draft opened before the migration.
4. Re-keys the pins and keeps every other `pangea.teacher_mode` field, `xp_to_complete_objective` included.
5. Leaves alone, and reports with the count of old ids it still names, a list readers already ignore: one that fails the app's strict parse, or whose sender is no longer at power level 100. The strict parse is the client's own, field for field; a `due_at` must be text with a `T` and then `Z` or a `±hh:mm` offset at its very end, that Dart's date parser reads (it rolls an out-of-range day or hour over, and refuses an instant past ±8.64e15 ms). A list with an unreadable date therefore stays ignored, never made readable by a rewrite. Pins that are not an object are left alone and reported as unreadable. Other pins are re-keyed whoever sent them, since the app reads them from any sender.
6. Sends the list, then the pins, as ordinary state events from the space's strongest local instructor, as [set-course-plan](set-course-plan.instructions.md) does, so readers accept the list and the [analytics forwarder](analytics-forward.instructions.md) updates the dashboard's mirror. Nobody joins and no power level changes.
7. Writes only while the space still holds both events its dry run read. A teacher's edit always makes a new event, so an edited space is skipped and reported, never overwritten. A re-run reads current state, so it never undoes an edit, and a space with no old id left has nothing to write. The check and the send are not atomic: a publish in the moment between them is lost.

After the switch, a list or pins event that still names an old Can-Do is accepted as sent and reported to Sentry by the stale progress guard (`synapse_pangea_chat/stale_progress_guard/`); a later dry run finds it.

## Combining a live course's entries

- **Place.** The combined entry takes the first occurrence's place. Occurrences directly after it form one span; a later occurrence with other entries between gives up its slot.
- **Due date.** The span's last date; when the last has none, the latest date before it in the span. Earlier dates in the span, and the date of every occurrence that gave up its slot, are dropped.
- **Activities.** `activity_ids` keeps the first entry's order, then adds the others' new ids; `removed_activity_ids` and `deleted_activity_ids` pool the same way. An activity one entry shows and another hid is shown.
- **Note.** Distinct non-blank notes join with " / ". Over 280 UTF-16 units, as the app counts, the first note is kept with "(+N more)", cut to fit if it is long itself, and the rest is dropped. An entry left with no note gets its old Can-Dos' statements as one, by the rule the quests follow (Will, 2026-11-13): in entry order, joined and cut the same way, leaving out a statement that only repeats the merged Can-Do's own and the old Can-Dos on the quest step's no-note list. An entry with a note, or combined with one that has, gains no old wording.
- **City and other fields.** `city` comes from the first entry that has one; every field this module does not model comes from the first entry. Other entries' values are dropped.
- **Removed Can-Dos.** They combine the same way, what each showed at removal first. When the course still shows the merged Can-Do, a removed old one's activities join that entry as hidden, still credited, and it can no longer be restored as its own Can-Do.
- **Pins.** Pins that land on one Can-Do pool. An old Can-Do the course listed with no pins adds nothing, so its activities stop showing under the now-pinned merged Can-Do. A pin value that is not a list of ids is dropped.

## What a teacher loses, and how often

Every rule above that drops something is counted in the response, so the operator sees it before applying: due dates, notes cut, cities and other fields, hidden activities now shown, removed Can-Dos folded into a shown one, old Can-Dos left unpinned, pin values dropped. On staging (2026-11-14, 48 of 1034 spaces rewritten) the report recorded 37 combined entries with 25 slots given up, no notes cut, no removed Can-Dos combined or folded, and 1 pin set pooled; it did not yet count dropped dates, cities, fields, hidden activities or unpinned Can-Dos. Whether each loss is acceptable for production is open on #832.

## Reporting

Responses, logs and Sentry carry ids, counts, lengths and error types, never a teacher's words: a trimmed note's text stays in the room's history, in the event the dry run read. Every path that leaves a space unmoved or drops a setting reaches Sentry under one fixed message per kind: a list left with old ids, unreadable pins, a rewrite the app would refuse, a space changed since its dry run, no local instructor, a refused or failed write, and a written combination that left settings out.

## The operator script

`scripts/migrate_course_can_dos.py` dry-runs every space on the homeserver, or the ones named, and writes a report to read. `--apply` then writes only the spaces the report says would change, each with its dry run's `expected`, and refuses a report made on another homeserver. Reports name rooms: keep them out of the repo. The nightly progress pass runs this dry run every night and never applies it; a space left to rewrite, or more than five refused or left with old ids, stops the run for a person ([migrate-course-progress](migrate-course-progress.instructions.md#the-nightly-pass)).

## Not in scope

- **Activity copies** (activity rows with `course_room_id`). Production has none, and staging's were deleted (#832). How the migration treats such copies is open on #832.
- **The dashboard's stores** (caches, stored first-completion dates, open drafts) move on its side, in the order the migration's deploy note gives.
- **Learners' progress** moves right after, in [migrate-course-progress](migrate-course-progress.instructions.md).
