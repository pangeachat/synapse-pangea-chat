---
applyTo: "synapse_pangea_chat/migrate_course_can_dos/course_progress.py,scripts/nightly_can_do_progress.py"
description: "One-off operator endpoint that moves each learner's course progress record from old (pre-merge) Can-Do ids to the merged Can-Dos, sent as the learner, without losing a completion; and the nightly pass that repeats it."
---

# Migrate Course Progress

Part of step 8 of the Can-Do migration ([pangeachat/.github#832](https://github.com/pangeachat/.github/issues/832)). A learner's progress in a course is a `pangea.course_progress` state event in their analytics room, keyed by course room ID; the dashboard shows its `can_dos` as the app's own figures. This server-side operation (Gabby, 2026-11-11) moves each record to the merged Can-Dos, right after the course spaces ([migrate-course-can-dos](migrate-course-can-dos.instructions.md)) and in later passes.

## Contract

`POST /_synapse/client/pangea/v1/migrate_course_progress`, server admins only. It takes the environment's old-to-merged id map, `dry_run`, and a page (`after`, `limit` up to 200). It answers, per record, an outcome (`rewrite`, `unchanged`, `unreadable`, `deactivated`, `refused`, `not_learners_own`, `not_local`, `failed`) with counts of keys moved, completions kept and restored, old keys left and unreadable dates, then page totals.

## What it does

1. A record that is not an object with a `can_dos` object is `unreadable`, as no reader reads it.
2. A record the learner (the room's creator) did not send is left alone, reported `not_learners_own` when it names an old id: the dashboard counts only the learner's own record.
3. Each old key moves to its merged Can-Do in its place. An old entry that is not an object stays and is counted.
4. A merged Can-Do the record no longer names is restored, at the end, when an earlier version the learner sent holds a completion of it, even one a teacher has since removed (Will, 2026-11-15).
5. **XP: the larger wins** (Will, 2026-11-15). Figures come whole from one entry, never summed: the record's own merged entry, even with less XP; else the old entry with the most XP, first on a tie; else, when restored, the earlier entry with the most XP. The app recomputes them on its next write.
6. The earliest first completion anywhere is kept, with its threshold and replay mark, and the Can-Do is completed if any entry it stands for ever was. A completion with no date anywhere gets the replay mark and no date, so the app dates it from history; a made-up date could never be corrected.
7. Everything else is left as it is. The record is sent as the learner, about ten a second, notifying nobody ([analytics-room-push-suppression](analytics-room-push-suppression.instructions.md)).
8. A learner on another server, or a room that refuses the send, is skipped and reported. A deactivated learner's record keeps its old ids and is counted on its own, never toward a failure limit: nobody reads or writes that room again, and the dashboard moves its stored figures itself ([admin-dash-api#494](https://github.com/pangeachat/admin-dash-api/pull/494)). Nothing is redacted.
9. Logs and Sentry carry ids, counts and error types, never content. A refused or failed send is reported with its room and course; a written page that left anything unmoved, deactivated records aside, is reported with its counts.

A dry run sends nothing and reports no page.

## Re-runs and the later passes

A run merges from the record and its history, so a second run changes nothing. An app that loaded a course before the switch can write old ids back until the course list's `revision` changes or it restarts. The stale progress guard (`synapse_pangea_chat/stale_progress_guard/`) corrects such writes once the map is loaded; the later passes catch the rest: nightly for two weeks, before the old Can-Dos are deleted, and a few weeks after. The map is kept until then. A read and its send are not atomic: an app write between them is replaced, and the app restores its figures next time.

## The nightly pass

A scheduled workflow runs the pass nightly, and on demand, for two weeks after a switch (Will, 2026-11-15): staging through 2026-11-28. Production is built but off: no nightly window until a person sets one on its switch night, so until then it runs only by hand, and never before its inputs are committed, which its switch writes. It dry-runs the course spaces without applying them, dry-runs the progress records, applies only when some need it, then dry-runs again. With the guard live, a rewrite found means a stale write got past it. Its inputs sit in a temporary folder deleted with these endpoints.

- **Counts only:** the Actions logs are public, so no learner, room or note is printed, and an error shows only its type.
- **It fails for a person** on a failed call; a space still naming old Can-Dos (that apply stays manual); more than five spaces refused or left with old ids; more than five failed or refused records in a run; a second dry run that still finds rewrites; or more than five records in the last dry run left with old ids or a lost completion (refused, remote, not the learner's own, failed, or an old key left). Deactivated learners' records are a separate count.
- **Credentials:** the admin token is minted in memory from that environment's bot credentials, through a role limited to that secret and this repository's main branch, on a fixed device per environment that is never logged out.

The operator script's progress apply exits non-zero on the same undone records, listing deactivated ones apart.
