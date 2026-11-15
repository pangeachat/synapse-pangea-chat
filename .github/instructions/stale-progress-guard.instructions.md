---
applyTo: "synapse_pangea_chat/stale_progress_guard/**,tests/test_stale_progress_guard*"
description: "How the homeserver corrects a learner's course progress write that still names retired Can-Do ids, so an app that loaded a course before the Can-Do switch cannot undo the migration."
---

# Stale Progress Guard

Part of the Can-Do migration ([pangeachat/.github#832](https://github.com/pangeachat/.github/issues/832)). After an environment's courses move to the merged Can-Dos, a learner's app that loaded a course's outline before the switch keeps the old Can-Do ids until it restarts. On its next analytics upload it writes the learner's progress record (`pangea.course_progress`) under the old ids, without the completion dates the migration kept ([migrate-course-progress](migrate-course-progress.instructions.md)). A restart does not repair the record. The guard stops such a write from replacing the migrated record. It does nothing until an environment's map is loaded, and goes with the migration's endpoints.

## What the learner and teacher see

The homeserver accepts the write and stores a corrected record in its place, as the learner's own event. The learner sees nothing: the send succeeds, so the app shows and reports no error. The app shows the old outline until it restarts; the guard cannot change that. The teacher's dashboard keeps the migrated Can-Dos, completions and dates, and gains a completion the learner reached on the stale app. XP the learner earned on the stale app reaches the dashboard after the app restarts and computes the record again.

Rejecting the write was rejected (Will, 2026-11-15). Today's app reports a refused write to Sentry, retries it after every upload, and stops checking the learner's other courses in that language for that run, so a refusal would also stop the dashboard's figures for courses that were never stale. Repairing after the write was rejected too: the stale record would be stored and forwarded to the dashboard before the repair.

## The correction

A write is corrected only when its `can_dos` names a retired Can-Do id. Any other write, including one from an up-to-date app that drops a Can-Do the teacher removed, is stored as sent. For a stale write, each Can-Do is keyed by its merged id, and:

1. Its figures come from the write's own entry under the merged id, else the stored entry under it, else the old entry, stored or written, with the most XP.
2. Its first completion is the earliest one found among all of them, with the threshold and the replay mark stored beside that date, as the migration keeps it.
3. It is completed whenever any of them was or a date is kept.
4. A Can-Do the stored record holds and the stale write left out is kept.
5. The rest of the record is the write's: when and by which app version it was computed, the XP that completes a Can-Do, and fields the guard does not know.

The correction is made before the event is stored, on whichever worker creates it, from the room's current state, so no stale record is ever stored or forwarded. Each correction is logged with ids and counts, never content.

## Course lists and pins

A course list (`pangea.course_missions`) or pins event (`pangea.teacher_mode`) naming a retired id is accepted as sent, logged and captured to Sentry with its type, room and count. Content Studio already refuses a publish from a draft opened before the switch, because the migration bumps the list's revision. Correcting a list would repeat the migration's combining rules, and refusing pins would show a teacher an error. A count above zero after a switch is a new decision.

## The map

The guard reads the environment's map of old Can-Do row id to merged row id, the same map the migration takes. It is stored in its own table in the homeserver's database, so every worker reads one map and it outlives the deletion of the old Can-Dos. A server admin loads it at `/_synapse/client/pangea/v1/retired_can_dos`: `PUT` replaces the whole map, `GET` returns it, and `DELETE` clears it, which turns the guard off. No deploy is needed. Each process re-reads the table at most once a minute.

Load the map right after the environment's progress migration, never before its switch. Before the switch the old ids are the current ones, and every normal write would be rewritten. Clear it when the migration's endpoints are removed.

## Cost and failure

The homeserver runs this check on every event it creates. For any other event type the guard returns after one comparison. It is registered after the module's other checks of this kind, because Synapse stops at the first check that replaces an event.

If the check fails, the write is stored as sent, and the error is captured to Sentry with its type only. Raising would refuse the learner's send with a server error and lose the upload. The nightly pass repairs a record that got through.
