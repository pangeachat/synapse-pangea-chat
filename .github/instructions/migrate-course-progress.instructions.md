---
applyTo: "synapse_pangea_chat/migrate_course_can_dos/course_progress.py"
description: "One-off operator endpoint that moves each learner's course progress record from old (pre-merge) Can-Do ids to the merged Can-Dos, sent as the learner, without losing a completion."
---

# Migrate Course Progress

Part of step 8 of the Can-Do migration ([pangeachat/.github#832](https://github.com/pangeachat/.github/issues/832)). A learner's progress in a course is a `pangea.course_progress` state event in their analytics room, with the course room ID as its state key. Its `can_dos` are keyed by Can-Do id, and the dashboard shows them as the app's own figures. This operation moves each record to the merged Can-Dos. It runs on each environment right after the course spaces ([migrate-course-can-dos](migrate-course-can-dos.instructions.md)), then in the later passes below, and is removed with that operation after the last. It runs on the server rather than in the app (Gabby, 2026-11-11).

## Contract

- `POST /_synapse/client/pangea/v1/migrate_course_progress`, server admins only.
- **Input:** the same map of old Can-Do row id to merged row id, `dry_run`, and a page: `after` (the previous page's `next`) and `limit` (at most 200).
- **Response:** for each record in the page: its room, course and learner; its outcome (`rewrite`, `unchanged`, `deactivated`, `refused`, `not_learners_own`, `not_local` or `failed`); how many old keys moved and how many completions were kept. Then `next`.

## What it does

1. Reads every `pangea.course_progress` in an analytics room, one page at a time.
2. Leaves alone, and reports, a record the learner did not send. The learner is the room's creator. The dashboard counts only the learner's own record; sent by the learner, this one would start to count.
3. Moves each old key to its merged Can-Do, in the old key's place, and removes the old key. A merged Can-Do is written only when the record names it, or names an old id of it. Its XP figures stay as the record has them under the merged id. Otherwise they come from the old entry with the most XP, and the app recomputes them on its next write.
4. Gives the merged Can-Do the earliest first completion it finds, with the threshold and the replay mark stored beside that date. The merged Can-Do is completed whenever any old entry it stands for was or a first completion is kept, so a record never says "not completed" beside a first completion; the app writes a dated entry as completed too. It looks in the current record and in every earlier version the learner sent, under both the old ids and the merged id. After a course switches, a learner's app rebuilds the record from the Can-Dos the course lists and drops the old entries with their dates; the earlier versions still hold them.
5. Leaves every other field and entry as it is.
6. Sends the record as the learner in an ordinary state event, at about ten records a second. The dashboard then counts it, and the analytics forwarder delivers it. It notifies nobody, because no push rule matches the event type ([analytics-room-push-suppression](analytics-room-push-suppression.instructions.md)).
7. Skips and reports a deactivated learner, a learner on another server, and a room that refuses the send.
8. Logs counts and ids, never a record's content.

A dry run does everything except the sends.

## Re-runs and the later passes

Every run merges from the current record and its history, so a second run changes nothing. An app that loaded a course's outline before the switch keeps the old ids until the `revision` of the course's Can-Do list changes or the app restarts, and in that time it can write old ids back. The course-space migration bumps the revision. A course without a Can-Do list of its own gives the app no such signal. Once the environment's map is loaded into the stale progress guard (`synapse_pangea_chat/stale_progress_guard/`), the homeserver corrects such a write before storing it, keeping the record's Can-Dos and dates. The later passes are then a safety net, for a write made before the map was loaded or one the guard let through because its check failed. They merge those records again from history: nightly for two weeks (below), right before the old Can-Dos are deleted, and a few weeks after the deletion. The environment's map is kept until that last pass. Reading a record and sending it are not one atomic step: an app write in the moment between them is replaced, and the app restores its figures on its next write.

## The nightly pass

For two weeks after an environment's switch, a scheduled workflow in this repository runs the pass nightly, and can be started by hand (Will, 2026-11-15). Only staging is configured, through 2026-11-28; production is a separate decision. With the guard live it is a safety net, and its end date stays as set: a run that finds rewrites after the map is loaded means a stale write got past the guard. Its map and statements are committed in a temporary folder deleted with these endpoints. It dry-runs the course spaces without applying them, dry-runs the progress records, applies only when some need rewriting, then dry-runs again.

- **Counts only.** Reports name learners and quote teachers' notes, and this repository's Actions logs are public, so nothing else is printed, written or uploaded; an error shows only its type.
- **It fails for a person** on a failed call, more than five failed or refused records (nothing is applied), a course space still naming old Can-Dos (the space apply stays manual), or a second dry run that still finds rewrites.
- **Credentials.** The admin token is minted in memory from the bot's credentials, read through a role limited to that one secret and this repository's main branch.

## Not in scope

- The dashboard's stored first-completion dates. They move on the dashboard's side, in the order the migration's deploy note gives.
