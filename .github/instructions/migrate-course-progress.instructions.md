---
applyTo: "synapse_pangea_chat/migrate_course_can_dos/course_progress.py,scripts/nightly_can_do_progress.py"
description: "One-off operator endpoint that moves each learner's course progress record from old (pre-merge) Can-Do ids to the merged Can-Dos, sent as the learner, without losing a completion; and the nightly pass that repeats it."
---

# Migrate Course Progress

Part of step 8 of the Can-Do migration ([pangeachat/.github#832](https://github.com/pangeachat/.github/issues/832)). A learner's progress in a course is a `pangea.course_progress` state event in their analytics room, with the course room ID as its state key. Its `can_dos` are keyed by Can-Do id, and the dashboard shows them as the app's own figures. This operation moves each record to the merged Can-Dos. It runs on each environment right after the course spaces ([migrate-course-can-dos](migrate-course-can-dos.instructions.md)), then in the later passes below, and is removed with that operation after the last. It runs on the server rather than in the app (Gabby, 2026-11-11).

## Contract

- `POST /_synapse/client/pangea/v1/migrate_course_progress`, server admins only.
- **Input:** the same map of old Can-Do row id to merged row id, `dry_run`, and a page: `after` (the previous page's `next`) and `limit` (at most 200).
- **Response:** for each record in the page: its room, course and learner; its outcome (`rewrite`, `unchanged`, `unreadable`, `deactivated`, `refused`, `not_learners_own`, `not_local` or `failed`); old keys moved, completions kept, completions restored, old keys left in place, and unreadable dates passed over. Then the page's counts of each, and `next`.

## What it does

1. Reads every `pangea.course_progress` in an analytics room, one page at a time. A record that is not an object with a `can_dos` object is `unreadable`, as neither the app nor the dashboard reads it.
2. Leaves alone a record the learner did not send, reported as `not_learners_own` when it names an old id. The learner is the room's creator. The dashboard counts only the learner's own record; sent by the learner, this one would start to count.
3. Moves each old key to its merged Can-Do, in the old key's place, and removes the old key. An old entry that is not an object is left in place and counted.
4. Restores a merged Can-Do the record no longer names at all, when an earlier version the learner sent holds a completion of it, under an old id or the merged id: a completion is never lost because an app with a reduced outline left the entry out. It goes at the end of the record.
5. **XP figures, as they are today.** The merged entry's figures (XP, its parts, skills, and every other field) are taken whole from one entry, never added up or recomputed: the record's own entry under the merged id, even with less XP than an old one; else the old entry with the most XP, the first in the record on a tie; else, for a restored Can-Do, the earlier entry with the most XP. The app recomputes them on its next write. Whether to keep this, sum, or mark for recompute is open on #832.
6. Gives the merged Can-Do the earliest first completion it finds, in the current record and in every earlier version, with the threshold and the replay mark stored beside that date. It is completed whenever any entry it stands for, now or earlier, was completed or dated, so a record never says "not completed" beside a first completion. A completion found with no date anywhere (an early app wrote `completed` alone) gets no date and the replay mark, so the app dates it from the learner's history, as it does any completion it never saw; a made-up date, such as the migration's own day, could never be corrected.
7. Leaves every other field and entry as it is.
8. Sends the record as the learner in an ordinary state event, at about ten records a second. The dashboard then counts it, and the analytics forwarder delivers it. It notifies nobody, because no push rule matches the event type ([analytics-room-push-suppression](analytics-room-push-suppression.instructions.md)).
9. Skips and reports a deactivated learner, a learner on another server, and a room that refuses the send. A deactivated learner's record keeps its old ids and is counted on its own, never toward a failure limit: it sits in a room the learner has left, which no app or account reads or writes again, and the dashboard's stored figures for that student are moved on the dashboard's side ([admin-dash-api#494](https://github.com/pangeachat/admin-dash-api/pull/494)). No record is redacted.
10. Logs and reports ids, counts and error types, never a record's content. A refused or failed send reaches Sentry with its room, course and error type; a written page that left anything unmoved, deactivated learners' records aside, reaches Sentry with its counts.

A dry run does everything except the sends and the page's Sentry report.

## Re-runs and the later passes

Every run merges from the current record and its history, so a second run changes nothing. An app that loaded a course's outline before the switch keeps the old ids until the `revision` of the course's Can-Do list changes or the app restarts, and in that time it can write old ids back. The course-space migration bumps the revision. A course without a Can-Do list of its own gives the app no such signal. Once the environment's map is loaded into the stale progress guard (`synapse_pangea_chat/stale_progress_guard/`), the homeserver corrects such a write before storing it, keeping the record's Can-Dos and dates. The later passes are then a safety net, for a write made before the map was loaded or one the guard let through because its check failed. They merge those records again from history: nightly for two weeks (below), right before the old Can-Dos are deleted, and a few weeks after the deletion. The environment's map is kept until that last pass. Reading a record and sending it are not one atomic step: an app write in the moment between them is replaced, and the app restores its figures on its next write.

## The nightly pass

For two weeks after an environment's switch, a scheduled workflow in this repository runs the pass nightly, and can be started by hand (Will, 2026-11-15). Only staging is configured, through 2026-11-28; production is a separate decision. With the guard live it is a safety net, and its end date stays as set: a run that finds rewrites after the map is loaded means a stale write got past the guard. Its map and statements are committed in a temporary folder deleted with these endpoints. It dry-runs the course spaces without applying them, dry-runs the progress records, applies only when some need rewriting, then dry-runs again.

- **Counts only.** Reports name learners, and this repository's Actions logs are public, so nothing else is printed, written or uploaded; an error shows only its type.
- **It fails for a person** on a failed call; a course space still naming old Can-Dos (the space apply stays manual); more than five course spaces refused or left with old ids; more than five failed or refused records in a run (in the dry run, nothing is applied); a second dry run that still finds rewrites; or more than five records left with old ids or a lost completion in the last dry run (refused, on another server, not the learner's own, failed, or an old key left in place), whether or not anything was applied. Deactivated learners' records are reported as their own count and never fail it.
- **Credentials.** The admin token is minted in memory from the bot's credentials, read through a role limited to that one secret and this repository's main branch.

The operator script's progress apply exits non-zero on any record left undone in that sense, and lists deactivated learners' records apart.

## Not in scope

- The dashboard's stored first-completion dates. They move on the dashboard's side, in the order the migration's deploy note gives.
