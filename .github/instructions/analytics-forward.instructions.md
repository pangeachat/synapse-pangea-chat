---
applyTo: "synapse_pangea_chat/analytics_forward/**,tests/test_analytics_forward*,tests/mock_analytics_ingest.py,tests/fixtures/analytics_forward/**"
description: "What the analytics forwarder sends to the teacher dashboard, from which rooms, and the guarantees it keeps."
---

# Analytics Forward — Synapse Module

Synapse sends the teacher dashboard's Matrix data to admin-dash-api as it happens, so a Refresh reads the dashboard's own database instead of asking Matrix with the teacher's login. This is what makes Refresh fast at scale. Staging only until reviewed; production is a separate decision.

## The experience

1. A teacher clicks Refresh. The dashboard waits only until everything sent up to that moment has arrived (usually no wait), then shows current data.
2. A student allows a teacher's analytics request. The teacher is joined to the student's analytics room automatically, with no action from the teacher.
3. Chat is never slowed or broken by this module, even when the dashboard is down.

## Decisions

- **Only the rooms the dashboard reads are sent:** student analytics rooms, course spaces, and rooms a course links both ways. Nothing from a DM or any other room. Reason: privacy. Re-decided by: Will.
- **Nothing is lost in normal operation:** an event counts as delivered only once the dashboard confirms it saved it; until then it is retried. Accepted gap: a room unlinked from its course while its events are still undelivered has them skipped. Re-decided by: engineer.
- **Delivery runs in the background at a steady pace,** so an outage or a backlog never slows Synapse. Re-decided by: engineer.
- **Refresh waits for delivery to catch up,** so a teacher never sees data older than their click. Re-decided by: Will.
- **Profile changes are sent too.** Accepted gap: a crash at the moment of a profile change can lose that change until the student's next change or the nightly reconcile. Re-decided by: engineer.
- **The teacher is joined only when** the student invited them into the student's own analytics room, for a course the teacher runs, and no other student claims that room. A knock is never accepted; when in doubt, it does not join. Re-decided by: Will.
- **No on/off switch:** it runs where it is configured. Deploy order is the safety: admin-dash-api first, then this config. Re-decided by: engineer.
- **Out of scope:** federated rooms, old history loaded later, and deletions after delivery (the nightly reconcile handles these). Re-decided by: engineer.

## Open questions

- Once this is live on staging, Refresh's two remaining Synapse reads (course members, pending invites) can be removed. Waits on Brord's review and deploy.
