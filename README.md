# Pangea Chat Module for Synapse

Unified [Synapse](https://github.com/element-hq/synapse) module that bundles all Pangea Chat server-side features into a single installable package.

## Modules

| Module                                        | Subpackage                                  | Endpoint                                                  | Description                                       |
| --------------------------------------------- | ------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------- |
| [Public Courses](#public-courses)             | `synapse_pangea_chat/`                      | `GET /_synapse/client/pangea/v1/public_courses`           | Course catalog with filtering and pagination      |
| [Room Preview](#room-preview)                 | `synapse_pangea_chat/room_preview/`         | `GET /_synapse/client/unstable/org.pangea/room_preview`   | Read room state events without membership         |
| [Activity Session Previews](#activity-session-previews) | `synapse_pangea_chat/activity_session_previews/` | `GET /_synapse/client/pangea/v1/activity_session_previews` | Previews of activity-session rooms under the caller's course spaces |
| [Room Code](#room-code)                       | `synapse_pangea_chat/room_code/`            | `POST /_synapse/client/pangea/v1/knock_with_code`         | Secret-code-based room invitations                |
|                                               |                                             | `GET /_synapse/client/pangea/v1/request_room_code`        | Generate a unique room access code                |
| [Delete Room](#delete-room)                   | `synapse_pangea_chat/delete_room/`          | `POST /_synapse/client/pangea/v1/delete_room`             | Room deletion for highest-power-level members     |
| [Delete User](#delete-user)                   | `synapse_pangea_chat/delete_user/`          | `POST /_synapse/client/pangea/v1/delete_user`             | Delete user associations then deactivate account   |
| [Find User By Email](#find-user-by-email)     | `synapse_pangea_chat/find_user_by_email/`   | `POST /_synapse/client/pangea/v1/find_user_by_email`      | Server-admin lookup of the account holding an email |
| [Course Member Emails](#course-member-emails) | `synapse_pangea_chat/course_member_emails/` | `POST /_synapse/client/pangea/v1/course_member_emails`    | A course admin reads their joined students' sign-in emails (off by default) |
| [Direct Message](#direct-message)             | `synapse_pangea_chat/direct_message/`       | `POST /_synapse/client/pangea/v1/ensure_direct_message`   | Create or repair a 1:1 DM for two local users     |
| [Analytics Forward](#analytics-forward)       | `synapse_pangea_chat/analytics_forward/`    | `GET /_synapse/client/pangea/v1/analytics_forward/head`, `POST /_synapse/client/pangea/v1/analytics_state` | Pushes the dashboard's Matrix inputs from course and analytics rooms to admin-dash-api (runs when configured) |
| [Delayed Push](#delayed-push)                 | `synapse_pangea_chat/delayed_push/`         | _(HttpPusher monkey patch)_                               | Delay HTTP pushes while users are active          |
| [Limit User Directory](#limit-user-directory) | `synapse_pangea_chat/limit_user_directory/` | _(spam checker)_                                          | Filter user directory by public profile attribute |

## Installation

From the virtual environment that you use for Synapse, install this module with:

```shell
pip install path/to/synapse-pangea-chat
```

Then alter your homeserver configuration, adding to your `modules` configuration:

```yaml
modules:
  - module: synapse_pangea_chat.PangeaChat
    config:
      # --- Public Courses ---
      public_courses_burst_duration_seconds: 120 # default: 120
      public_courses_requests_per_burst: 120 # default: 120
      course_plan_state_event_type: "pangea.course_plan" # default: null (falls back to pangea.course_plan)
      public_courses_cms_cache_ttl_seconds: 5 # default: 5s — short burst cache; refreshes quickly
      central_course_admin_user_ids: [] # default: [] (trusts @support:<server name>)

      # --- Room Preview ---
      room_preview_state_event_types: # additional state event types to expose
        - "p.room_summary"
        - "pangea.activity_plan"
        - "pangea.activity_roles"
      room_preview_burst_duration_seconds: 60 # default: 60
      room_preview_requests_per_burst: 10 # default: 10

      # --- Room Code ---
      knock_with_code_requests_per_burst: 10 # default: 10
      knock_with_code_burst_duration_seconds: 60 # default: 60

      # --- Delete Room ---
      delete_room_requests_per_burst: 10 # default: 10
      delete_room_burst_duration_seconds: 60 # default: 60

      # --- Delete User ---
      delete_user_requests_per_burst: 5 # default: 5
      delete_user_burst_duration_seconds: 60 # default: 60
      delete_user_schedule_delay_seconds: 604800 # default: 7 days
      delete_user_processor_interval_seconds: 60 # default: 60

      # --- Find User By Email ---
      find_user_by_email_requests_per_burst: 10 # default: 10
      find_user_by_email_burst_duration_seconds: 60 # default: 60

      # --- Course Member Emails (disabled by default) ---
      course_member_emails_enabled: false # default: false
      course_member_emails_requests_per_burst: 20 # default: 20
      course_member_emails_burst_duration_seconds: 60 # default: 60

      # --- Limit User Directory (disabled when path is null) ---
      limit_user_directory_public_attribute_search_path: "profile.user_settings.public"
      limit_user_directory_filter_search_if_missing_public_attribute: true
      limit_user_directory_whitelist_requester_id_patterns:
        - "^@admin:example.com$"
      limit_user_directory_whitelist_candidate_user_id_patterns:
        - "^@bot:example.com$"

      # --- Analytics Forward (runs only when this block is present) ---
      analytics_forward:
        url: "https://admin-dash-api.example.com/api/internal/matrix-events"
        secret: "<at least 32 characters, shared with admin-dash-api>"
        batch_max: 200 # default: 200 (1-1000)
        poll_seconds: 5 # default: 5 (1-60)
        start_position: current # default: current; read only when no cursor is stored

      # --- Delayed Push (disabled by default) ---
      delayed_push:
        enabled: false
        delay_ms: 60000
        max_delay_ms: 600000
        require_synapse_version: "1.159.0"
```

All config keys are optional and have sensible defaults. The `limit_user_directory` spam checker is only activated when `limit_user_directory_public_attribute_search_path` is set.

---

## Public Courses

Surface curated course previews via a dedicated HTTP endpoint with built-in filtering and rate limiting.

**Canonical route:** `GET /_synapse/client/pangea/v1/public_courses`

**Compatibility route:** `GET /_synapse/client/unstable/org.pangea/public_courses`

### Authentication

Requires a valid Matrix access token; unauthenticated calls return `401 M_UNAUTHORIZED`. A simple in-memory burst rate limit protects the resource (default: 120 requests per user in a 120-second window, returns `429` when exceeded).

### Query Parameters

| Name              | Type    | Default | Description                                                                 |
| ----------------- | ------- | ------- | --------------------------------------------------------------------------- |
| `limit`           | integer | `10`    | Maximum number of courses to return                                         |
| `since`           | string  | `None`  | Pagination cursor (`next_batch`) returned by a previous call                |
| `target_language` | string  | `None`  | Filter by target language, read from `l2` on `pangea.course_plan` (e.g. `es`) |

`target_language` matches on **base language**: `es` matches `es`, `es-ES`, and `es-MX`, and the reverse. A room with no `l2` appears when no filter is passed and is excluded when one is.

There is no CMS call on the read path — the target language is carried on the room's own course-plan state event. There are no `language_of_instructions` or `cefr_level` filters; a client that wants a course's L1 or CEFR level reads them from the course plan it already fetches to render the card.

Filtering happens **before** pagination, so a page is full unless the catalog is exhausted and a non-null `next_batch` means more results genuinely exist. `next_batch` is a keyset cursor (the last room id of the page); a legacy decimal `since` from an older client is still accepted as an offset for one request.

### Response

```json
{
  "chunk": [
    {
      "room_id": "!abc:example.org",
      "name": "Course name",
      "topic": "Short description",
      "avatar_url": "mxc://example.org/asset",
      "canonical_alias": "#course:example.org",
      "num_joined_members": 0,
      "world_readable": false,
      "guest_can_join": false,
      "join_rule": null,
      "room_type": null,
      "course_id": "5f0c…",
      "target_language": "es"
    }
  ],
  "next_batch": "!lastRoomOnThisPage:example.org",
  "total_room_count_estimate": 23
}
```

`next_batch` is `null` on the last page.

### Room Selection Criteria

A room appears in the catalog if, and only if, **both** of the following are true:

1. `rooms.is_public` is true (it is published in the public room directory).
2. Its **current** room state carries the course-plan state event (default `pangea.course_plan`) with a plan id — read from `uuid`, falling back to `course_plan_id`.

Nothing else is checked: not member count, not join rule, and not the contents of the quest. A quest with zero missions is a published course and appears. Because only current state is consulted, a room that once had a course plan and no longer does is not a course.

---

## Room Preview

Allow authenticated users to read content of pre-configured state events from rooms without being a member.

**Route:** `GET /_synapse/client/unstable/org.pangea/room_preview`

### Query Parameters

| Name    | Type   | Default   | Description                      |
| ------- | ------ | --------- | -------------------------------- |
| `rooms` | string | _(empty)_ | Comma-delimited list of room IDs |

### Response

```json
{
  "rooms": {
    "!room_id:example.com": {
      "event_type": {
        "state_key": {
          /* full event JSON content */
        }
      },
      "membership_summary": {
        "@user_id:example.com": "join"
      }
    }
  }
}
```

#### Membership Summary

Included for rooms containing `pangea.activity_roles` (activity rooms) or `pangea.course_plan` (course rooms):

- **Activity rooms**: Only includes users referenced in activity roles, allowing clients to see roles of users who have left.
- **Course rooms**: Includes all users, allowing clients to track course membership state.

#### Content Filtering

`m.room.join_rules` state events are filtered to only include the `join_rule` key — all other keys (e.g. `allow` for restricted rooms) are stripped for security/privacy.

#### Caching

In-memory cache with 1-minute TTL. Cache is reactively invalidated when relevant state events change.

_Originally: [pangeachat/synapse-room-preview](https://github.com/pangeachat/synapse-room-preview)_

---

## Activity Session Previews

Space-scoped activity-session discovery: previews of the activity-session child rooms under the caller's joined course spaces, in the same response shape as [Room Preview](#room-preview). Design: [activity-session-previews.instructions.md](.github/instructions/activity-session-previews.instructions.md).

**Route:** `GET /_synapse/client/pangea/v1/activity_session_previews`

### Query Parameters

| Name       | Type   | Default   | Description                                      |
| ---------- | ------ | --------- | ------------------------------------------------ |
| `rooms`    | string | _(empty)_ | Comma-delimited list of course-space room IDs    |
| `activity` | string | _(none)_  | Optional activity ID to narrow the sessions to   |

### Behavior

- The caller must be a **joined member** of each requested space; non-member spaces are silently dropped.
- A session is a direct `m.space.child` carrying a `pangea.activity_plan` state event; removed children (no valid `via`) are excluded, and the suggested flag is ignored.
- Response shape, content filtering, membership summary, caching, and rate limiting are inherited from [Room Preview](#room-preview) — this endpoint is a thin front on the same reader.

---

## Room Code

Extend rooms to optionally have a secret code. Upon knocking with a valid code, the user is invited to the room.

### Knock With Code

**Route:** `POST /_synapse/client/pangea/v1/knock_with_code`

**Body:** `{ "access_code": "<7-char alphanumeric with ≥1 digit>" }`

**Response (200):**

```json
{
  "message": "string",
  "rooms": ["!room:example.com"],
  "already_joined": ["!other:example.com"]
}
```

### Request Room Code

**Route:** `GET /_synapse/client/pangea/v1/request_room_code`

**Response (200):**

```json
{ "access_code": "A1B2C3D" }
```

_Originally: [pangeachat/synapse-room-code](https://github.com/pangeachat/synapse-room-code)_

---

## Delete Room

Expose an endpoint for room admins (members with the highest power level) to kick everyone out, clean up space relationships, and purge the room.

**Route:** `POST /_synapse/client/pangea/v1/delete_room`

**Body:** `{ "room_id": "!room:example.com" }`

**Response (200):**

```json
{ "message": "Deleted" }
```

Requester must be a member of the room and have the highest power level.

_Originally: [pangeachat/synapse-delete-room-rest-api](https://github.com/pangeachat/synapse-delete-room-rest-api)_

---

## Direct Message

Create or repair a 1:1 DM between two local users without needing either user's access token.

**Route:** `POST /_synapse/client/pangea/v1/ensure_direct_message`

**Body:** `{ "user_ids": ["@alice:example.com", "@bob:example.com"] }`

Requester must be a Synapse server admin. The endpoint accepts exactly two distinct local user IDs, reuses an existing qualifying DM when possible, and repairs `m.direct`/power levels only when needed so clients treat the room as a DM. A valid existing DM returns as `valid_existing_noop` without write-side calls.

**Response (200):**

```json
{
  "room_id": "!room:example.com",
  "created": true,
  "reused": false,
  "m_direct_updated_for": [
    "@alice:example.com",
    "@bob:example.com"
  ],
  "power_levels_updated": false,
  "action": "created_room"
}
```

---

## Delayed Push

Optionally delays normal Matrix HTTP push notifications while the target user is actively using Synapse. This is disabled by default because it monkey-patches Synapse's private `HttpPusher` internals and must be re-audited for every Synapse version upgrade.

When enabled, normal HTTP pushers reschedule unread notifications for users whose Synapse presence `state` is `online` at the configured delay interval. If the event is read before the next check, Synapse's unread push-action query drops it and no notification is sent. If the user becomes offline/unavailable or the event reaches `max_delay_ms` age, the pusher sends normally; stale `currently_active=true` does not keep delaying an offline user. Call rings (`org.matrix.msc4075.rtc.notification`) are never delayed, and a ring queued behind a delayed notification sends it early, in order, along with the ring. DirectPush, email pushers, and badge-only receipt updates are unchanged.

### Config

| Key                                      | Type | Default | Description |
| ---------------------------------------- | ---- | ------- | ----------- |
| `delayed_push.enabled`                   | bool | `false` | Enables the HttpPusher monkey patch |
| `delayed_push.delay_ms`                  | int  | `60000` | Recheck interval while the user remains active |
| `delayed_push.max_delay_ms`              | int  | `600000` | Maximum event age before sending even if still active |
| `delayed_push.require_synapse_version`   | str  | `1.159.0` | Exact audited Synapse version required when enabled |

---

## Delete User

Schedule user deletion for one week in the future by default. Supports canceling or forcing the scheduled deletion.

**Route:** `POST /_synapse/client/pangea/v1/delete_user`

**Body (optional):**

```json
{
  "action": "schedule|cancel|force",
  "user_id": "@user:example.com"
}
```

- If `user_id` is omitted, the requester is deleted.
- Server admins may specify another local user ID.
- Non-admins can only delete themselves.
- `action` defaults to `schedule`.

Behavior by action:

- `schedule`: Creates/updates a deletion schedule for `now + 7 days` (or configured delay).
- `cancel`: Cancels an existing schedule.
- `force`: Immediately executes a previously scheduled deletion.

**Response (200):**

```json
{
  "message": "Delete scheduled",
  "action": "schedule",
  "user_id": "@user:example.com",
  "execute_at_ms": 1762790400000
}
```

---

## Find User By Email

Resolve an email address to the local account that has it bound. Synapse's admin user search matches only the user-ID localpart and display name, so it cannot answer this. Design: [find-user-by-email.instructions.md](.github/instructions/find-user-by-email.instructions.md).

**Route:** `POST /_synapse/client/pangea/v1/find_user_by_email`

**Server admins only** — non-admin callers get `403`, whether or not the address matches. POST rather than GET so the address is not written into access logs.

**Body:**

```json
{ "address": "person@school.edu" }
```

**Response (200):**

```json
{
  "address": "person@school.edu",
  "results": [
    {
      "user_id": "@jdoe:example.com",
      "address": "person@school.edu",
      "display_name": "Jane Doe",
      "deactivated": false
    }
  ]
}
```

Matching is case-insensitive and whole-address only — no domain or substring search. An address with no live account returns an empty `results`, not a 404. Synapse removes threepid bindings when an account is deactivated, so an empty result means "no live account", not "never registered".

---

## Course Member Emails

A course admin reads the sign-in email of each student currently in their course, for the teacher dashboard's Students page. Off by default (`course_member_emails_enabled`); when off, the route does not exist. Design: [course-member-emails.instructions.md](.github/instructions/course-member-emails.instructions.md).

**Route:** `POST /_synapse/client/pangea/v1/course_member_emails`

**Course admins only** (power level 100 in the course space, or a creator in room versions where creators hold unlimited power). Every refusal, including an unknown room or a caller who is not in it, is the same `403` with the same body. Rate limited per caller (default 20 calls per 60 seconds, `429` beyond).

**Body:**

```json
{ "room_id": "!course:example.com" }
```

**Response (200):**

```json
{ "members": [{ "user_id": "@student:example.com", "email": "student@school.edu" }] }
```

Lists members whose current membership is `join`, excluding the caller, bots, and other course admins. A member with no bound email is omitted. Addresses are never logged.

---

## Analytics Forward

Pushes the teacher dashboard's Matrix inputs to admin-dash-api's `POST /api/internal/matrix-events`, and joins a teacher to a student's analytics room once the student has consented. Design and decisions: [analytics-forward.instructions.md](.github/instructions/analytics-forward.instructions.md). Request and response contract, shared with admin-dash-api: [ingest_contract.json](tests/fixtures/analytics_forward/ingest_contract.json).

### What is sent

- **Types** (`wire.py`): construct uses (`pangea.construct`, as timeline events), plus these state types: membership, power, room identity, `m.room.canonical_alias`, course-space, session-room and analytics-room state, and `pangea.course_progress`. admin-dash-api's forwarded-types inventory test decides the list.
- **Rooms** (`ForwardStore.course_rooms`, decided from current state when each batch is read):
  - analytics rooms (`m.room.create` type `p.analytics`);
  - course spaces (`m.room.create` type `m.space` with a `pangea.course_plan`);
  - rooms a course links both ways: the room's `m.space.parent` names a course space, and that space holds the matching `m.space.child`, both with a non-empty `via`.

  Every lookup is by `(room_id, type, state_key)`. Nothing from any other room is sent or snapshotted. A room linked later is sent from then on; its earlier state reaches admin-dash-api through the snapshot endpoint.
- **Accepted gap:** a room unlinked from its course while its events are still undelivered (during an ingest outage, say) has those events skipped.

### Delivery

- **One loop, on the background-tasks instance only.** `on_new_event` fires on every worker, so a loop on each would send everything N times. The `pangea_analytics_forward_active` gauge is 1 there and 0 elsewhere.
- **The stream is the queue.** The loop reads Synapse's own `events` table above a durable cursor. Reads are oldest first, in batches of at most `batch_max`, of the forwarded types only, never outliers or rejected events. `on_new_event` only wakes the loop, and a timer wakes it every `poll_seconds` in case a wake-up is missed. Reads are a quarter second apart, catch-up included, so a backlog costs Synapse's database a few reads a second.
- **One connection per delivery.** No idle connection is kept: a pooled keep-alive connection the server closes just as a request is written on it can swallow that request.
  - A request that gets no answer is sent once more at once, on a new connection.
  - Each attempt is capped at 30 s, and a timed-out attempt aborts its connection.
  - Only the first 64 KiB of an answer are read; a bigger answer is refused, not re-sent.
- **The cursor moves only on an acknowledgement.** The ingest answers 200 only after committing the batch, echoing the batch's `delivered_through`.
  - Anything else (an error, a timeout, a 200 that does not echo it) leaves the cursor where it was.
  - The loop then retries the same events forever, backing off from 1 s, doubling to 60 s.
  - A restart resumes from the stored cursor, so a re-sent batch is the only duplicate. The ingest's writes are idempotent.
- **A wrong shared secret (401/403) is a configuration error.** It is logged as one and retried at the longest backoff until it is fixed.
- **The delivery watermark.** Every batch carries `delivered_through`, the position delivery reaches once that batch is committed. When idle, the loop still sends an empty batch every `poll_seconds`, so the ingest always knows how far delivery has reached.

### Profiles

Profiles are not room events. Synapse calls `on_profile_update` after the write, in its own transaction, so a profile change goes through an outbox:

- Each change marks the user's outbox row dirty and raises its revision. Every delivery sends the user's whole current profile with that revision. The acknowledgement clears the row only if the revision is unchanged, so a change made while a send was in flight is sent again.
- Every batch carries `profiles_clean_before`: every profile change older than it has been delivered. It is the oldest undelivered change, minus two seconds as a margin for the gap between the write and the outbox row.
- **On every start, before anything is sent,** every user in the outbox and every user with a `pangea.analytics_profile` is marked for re-send as "dirty since always". The heartbeat therefore reports profiles clean only after all of them are delivered again.
- **Accepted limitation:** a crash between a profile write and its outbox row loses that change until the user's next profile change or the nightly reconcile's snapshot.

### Consented analytics invites

A student who allows a teacher's analytics request invites the teacher into their analytics room, and nothing in the app completes the join. The module completes it under the same five-fact proof admin-dash-api used (`composition._accept_consented_analytics_invites`), read from this homeserver's current state:

1. The teacher currently holds an invite into the room.
2. The room is the one a student of a course the teacher administers declares in their analytics profile for the course language, and no other student of that course claims it.
   - A course is a space with a course plan.
   - Administering it means joined at power 100 or more.
3. That student sent the invite.
4. That student created the room.
5. The room is an analytics room (`m.room.create` type `p.analytics`).

Rules around the proof:

- A knock is never accepted.
- The course language is the course plan's `l2`. Where a course plan names none, every room in the student's profile is a candidate (admin-dash-api's own unknown-language rule), and facts 3–5 still have to hold.
- The proof is re-read before the join, so only the invite that was proven is accepted.
- Consent never fails open.

How the work runs:

- The proof and the join run in their own worker, never in the delivery path. Every invite into a forwarded analytics room is owed to it.
- Until the worker has answered, the invite is named in every batch (`pending_accepts`). Its `pending_accepts_rev` grows on every change and survives restarts.
- A successful join's position is the fence on the next batch (`accept_join_pos`).
- A failing or hanging join delays only its own invite. Failures are retried once per poll.
- Pending invites are read on start, before anything is sent (a failed read fails the start, which is retried), and again daily.
- On start, any self-join into an analytics room still above the cursor is fenced again. A crash between a join and its delivery therefore cannot release a Refresh early.

### Endpoints (shared secret)

Both take `Authorization: Bearer <analytics_forward.secret>`, compared in constant time.

- `GET /_synapse/client/pangea/v1/analytics_forward/head` returns a delivery barrier. admin-dash-api's Refresh does not use it today: Refresh does not wait for delivery. The barrier has two parts:
  - the newest forwarded event at or below the persisted head;
  - a profile time naming only changes not yet delivered.

  An idle forwarder therefore costs a reader no wait. Reading the head also wakes the forwarder when it runs in the same process, so anything undelivered goes at once rather than at the next poll.
- `POST /_synapse/client/pangea/v1/analytics_state` takes up to 500 rooms and 500 users and returns:
  - each room's complete current state of the forwarded types (null for a room that is unknown or outside the rooms above);
  - each user's profile, with a revision raised by the read, so a snapshot always outranks earlier pushes.

### Configuration

There is no on/off switch. The forwarder runs wherever the block is configured; without it, the module logs once that it is not configured and does nothing. Deploy order is the safety: admin-dash-api's ingest first, then this config. Configured before the ingest exists, the forwarder only backs off and its cursor never moves, so nothing is lost.

```yaml
analytics_forward:
  url: https://<admin-dash-api host>/api/internal/matrix-events  # https; plain http only to a loopback host
  secret: <at least 32 characters, from the vault; the same value admin-dash-api holds>
  batch_max: 200          # 1-1000
  poll_seconds: 5         # 1-60
  start_position: current # or a stream position; read only when no cursor is stored
```

PostgreSQL only. The module adds two tables to Synapse's database: `pangea_analytics_forward_cursor` and `pangea_analytics_profile_outbox`.

### Observability

- `pangea_analytics_forward_lag`: stream positions between the head and the cursor. Alert when it stays high for 10 minutes.
- `pangea_analytics_forward_active`: alert when its sum is 0 where the forwarder is configured.
- `pangea_analytics_forward_failures_total{reason}`: delivery failures.
- `pangea_analytics_forward_rejected_total`: events the ingest set aside as rejects.
- `pangea_analytics_forward_consent_total{verdict}`: consent answers.

### Out of scope

- Federated rooms.
- Backfilled events: negative stream positions are below every cursor.
- Redactions after delivery: the nightly reconcile handles them.

---

## Limit User Directory

Spam checker callback that filters the user directory based on a public profile attribute. Only users whose profile contains the configured attribute (at the dot-syntax path) set to `true` are visible in search results.

### Config

| Key                                                              | Type           | Default | Description                                                                                                            |
| ---------------------------------------------------------------- | -------------- | ------- | ---------------------------------------------------------------------------------------------------------------------- |
| `limit_user_directory_public_attribute_search_path`              | string \| null | `null`  | Dot-syntax path to the public boolean attribute (e.g. `profile.user_settings.public`). Module is disabled when `null`. |
| `limit_user_directory_filter_search_if_missing_public_attribute` | bool           | `true`  | Whether to filter users who lack the attribute entirely                                                                |
| `limit_user_directory_whitelist_requester_id_patterns`           | list[str]      | `[]`    | Regex patterns for user IDs that bypass filtering                                                                      |

Users sharing a room with the requester are always visible regardless of the public attribute.

_Originally: [pangeachat/synapse-limit-user-directory](https://github.com/pangeachat/synapse-limit-user-directory)_

---

### Tests

```shell
tox -e py          # or: trial tests
```

Tests require `postgres` installed locally (`which postgres` should return a path).

### Linting & Type Checking

```shell
./scripts-dev/lint.sh    # runs black, ruff, mypy
```

## Project Structure

```
synapse_pangea_chat/
├── __init__.py                  # Unified entry point (PangeaChat class)
├── __main__.py                  # CLI version check
├── config.py                    # Unified PangeaChatConfig
├── public_courses.py            # Public courses endpoint
├── get_public_courses.py        # Public courses query logic
├── is_rate_limited.py           # Public courses rate limiter
├── types.py                     # Shared types
├── room_preview/                # Room preview module
├── room_code/                   # Room code module
├── delete_room/                 # Delete room module
└── limit_user_directory/        # Limit user directory module
tests/
├── __init__.py                  # Shared test helpers
├── test_e2e.py                  # Public courses E2E tests
├── test_room_preview_e2e.py     # Room preview E2E tests
├── test_room_preview_reactive_cache.py
├── test_room_code_e2e.py        # Room code E2E tests
├── test_delete_room_e2e.py
└── test_limit_user_directory_e2e.py
```

## Development

In a virtual environment with pip ≥ 21.1, run

```shell
pip install -e .[dev]
```

To run the unit tests, you can either use:

```shell
tox -e py
```

or

```shell
trial tests
```

To view test logs for debugging, use:

```shell
tail -f synapse.log
```

To run the linters and `mypy` type checker, use `./scripts-dev/lint.sh`.

## Releasing

The exact steps for releasing will vary; but this is an approach taken by the
Synapse developers (assuming a Unix-like shell):

1.  Set a shell variable to the version you are releasing (this just makes
    subsequent steps easier):

    ```shell
    version=X.Y.Z
    ```

2.  Update `setup.cfg` so that the `version` is correct.

3.  Stage the changed files and commit.

    ```shell
    git add -u
    git commit -m v$version -n
    ```

4.  Push your changes.

    ```shell
    git push
    ```

5.  When ready, create a signed tag for the release:

    ```shell
    git tag -s v$version
    ```

    Base the tag message on the changelog.

6.  Push the tag.

    ```shell
    git push origin tag v$version
    ```

7.  If applicable:
    Create a _release_, based on the tag you just pushed, on GitHub or GitLab.

8.  If applicable:
    Create a source distribution and upload it to PyPI:
    ```shell
    python -m build
    twine upload dist/synapse_room_preview-$version*
    ```

## Local email previews

[Preview and capture notice emails locally](scripts/EMAIL_PREVIEW.md) for template and copy iteration without deploying or sending external mail.
