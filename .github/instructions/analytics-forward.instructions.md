---
applyTo: "synapse_pangea_chat/analytics_forward/**,tests/test_analytics_forward*,tests/mock_analytics_ingest.py,tests/fixtures/analytics_forward/**"
---

# Analytics Forward — Synapse Module

Pushes the Matrix inputs of the teacher dashboard to admin-dash-api, so a dashboard Refresh reads its own database instead of walking Matrix with the teacher's token. Lossless: nothing is dropped, and nothing is held only in memory. Part of the dashboard roll-ups plan (rev 6, PR S1); admin-dash-api's ingest is PR A1.

## Delivery

- **One loop, on the background-tasks instance only.** `on_new_event` fires on every worker; a loop on each would send everything N times. The `pangea_analytics_forward_active` gauge is 1 there and 0 elsewhere.
- **The stream is the queue.** The loop reads Synapse's own `events` table above a durable cursor, oldest first, in batches of at most `batch_max`, of the forwarded types only, never outliers or rejected events. `on_new_event` only wakes it, and a timer wakes it every `poll_seconds` in case a wake-up is missed. Reads are paced (a quarter second apart), catch-up included, so a backlog costs Synapse's database a few reads a second.
- **The cursor moves only on an acknowledgement.** The ingest answers 200 only after committing the batch, echoing the batch's `delivered_through`. Anything else (an error, a timeout, a 200 that does not echo it) leaves the cursor where it was, and the loop retries the same events forever with a backoff of 1 s doubling to 60 s. A restart resumes from the stored cursor, so a re-sent batch is the only duplicate, and the ingest's writes are idempotent.
- **A wrong shared secret (401/403) is a configuration error**, logged as one and retried at the longest backoff until it is fixed.
- **The delivery watermark.** Every batch carries `delivered_through`, the position delivery reaches once it is committed. When idle the loop still sends an empty batch every `poll_seconds`, so the ingest always knows how far delivery has reached; a Refresh waits for it to pass the head it read.
- **What is forwarded** is the construct uses (`pangea.construct`, as timeline events) and the state types the dashboard reads (membership, power, room identity, course-space, session-room and analytics-room state). The list is in `wire.py`; admin-dash-api's forwarded-types inventory test (plan PR A2) is what decides it.

## Profiles

Profiles are not room events, and Synapse calls `on_profile_update` after the write, in its own transaction, so a profile change goes through an outbox:

- Each change marks the user's row dirty and raises its revision. Every delivery sends the user's whole current profile with that revision; the acknowledgement clears the row only if the revision is unchanged, so a change made while a send was in flight is sent again.
- Every batch carries `profiles_clean_before`: every profile change older than it has been delivered. It is the oldest undelivered change, two seconds earlier as a margin for the write-to-outbox gap.
- **On every start, before anything is sent,** every user in the outbox and every user with a `pangea.analytics_profile` is marked for re-send as "dirty since always", so the heartbeat reports profiles clean only after all of them are delivered again. Refreshes in that window wait.
- **Accepted limitation:** a crash between a profile write and its outbox row loses that change until the user's next profile change or the nightly reconcile's snapshot.

## Consented analytics invites

A student who allows a teacher's analytics request invites the teacher into their analytics room, and nothing completes the join. The module completes it, under the same five-fact proof admin-dash-api used (`composition._accept_consented_analytics_invites`), read from this homeserver's current state:

1. the teacher currently holds an invite into the room;
2. the room is the one a student of a course the teacher administers (joined, power 100 or more) declares in their analytics profile for the course language, and no other student of that course claims it;
3. that student sent the invite;
4. that student created the room;
5. the room is an analytics room (`m.room.create` type `p.analytics`).

A knock is never accepted. The course language is the course plan's `l2`; where a course plan names none, every room in the student's profile is a candidate (admin-dash-api's own unknown-language rule); facts 3–5 still have to hold. The join happens before the batch carrying the invite is sent, and that batch carries the join's position (`accept_join_pos`) so a Refresh cannot finish before the join is delivered. The proof is re-read before the join, so only the invite that was proven is accepted. A join that fails never holds delivery: it is owed, retried on its own at most once per poll, and named in every batch (`pending_accepts`) until it succeeds, so the ingest holds that teacher's course Refresh while everything else keeps flowing. Invites already pending when the forwarder starts are checked at startup and then daily; a failure there is logged and the next scan retries. Consent never fails open.

## Endpoints (shared secret)

Both take `Authorization: Bearer <analytics_forward.secret>`, compared in constant time.

- `GET /_synapse/client/pangea/v1/analytics_forward/head` returns the persisted stream position and the module's clock, which a Refresh waits for.
- `POST /_synapse/client/pangea/v1/analytics_state` returns, for up to 500 rooms and 500 users, each room's complete current state of the forwarded types (null for an unknown room) and each user's profile with a revision raised by the read, so a snapshot always outranks earlier pushes.

The request and response contract, shared with admin-dash-api, is `tests/fixtures/analytics_forward/ingest_contract.json`.

## Configuration

There is no on/off switch: the forwarder runs wherever the block is configured, and without it the module logs once that it is not configured and does nothing. Deploy order is the safety: admin-dash-api's ingest first, then this config. Configured before the ingest exists, the forwarder only backs off and its cursor never moves, so nothing is lost.

```yaml
analytics_forward:
  url: https://<admin-dash-api host>/api/internal/matrix-events
  secret: <at least 32 characters, from the vault; the same value admin-dash-api holds>
  batch_max: 200          # 1-1000
  poll_seconds: 5         # 1-60
  start_position: current # or a stream position; read only when no cursor is stored
```

PostgreSQL only. The module adds two tables to Synapse's database: `pangea_analytics_forward_cursor` and `pangea_analytics_profile_outbox`.

## Observability

- `pangea_analytics_forward_lag`: stream positions between the head and the cursor. Alert when it stays high for 10 minutes.
- `pangea_analytics_forward_active`: alert when its sum is 0 where the forwarder is configured.
- `pangea_analytics_forward_failures_total{reason}`, `pangea_analytics_forward_rejected_total` (events the ingest set aside as rejects), `pangea_analytics_forward_consent_total{verdict}`.

## Out of scope

Federated rooms; backfilled events (negative stream positions are below every cursor); redactions after delivery (the nightly reconcile handles them).
