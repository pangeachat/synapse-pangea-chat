---
applyTo: "synapse_pangea_chat/user_activity/**"
---

# User Activity Endpoints

Four Synapse module endpoints that power the engagement runner (three reads and the refresh of the kept activity table). Called by the [bot engagement doc](../../../pangea-bot/.github/instructions/engagement.instructions.md).

## Endpoints

### 1. Users (paginated)

`GET /_synapse/client/pangea/v1/user_activity`

- **Auth:** Matrix bearer token, server admin required (403 if not admin)
- **Rate limiting:** None (admin-only, no rate limiting)
- **Query params:** `page` (int, default 1), `limit` (int, default 50, max 200), `sort_by` (`user_id`, `last_login_ts`, `last_message_ts`, or `latest_activity`; default `user_id`), `sort_order` (`asc` or `desc`; default `asc`)

**Response:**

```json
{
  "docs": [
    {
      "user_id": "@alice:example.com",
      "display_name": "Alice",
      "last_login_ts": 1700000000000,
      "last_message_ts": 1700000000000,
      "latest_activity_ts": 1700000000000
    }
  ],
  "page": 1,
  "limit": 50,
  "totalDocs": 200,
  "maxPage": 4
}
```

User docs contain only basic activity metadata. `latest_activity_ts` is `max(last_login_ts, last_message_ts)`. Defaults preserve historical `user_id` ascending order; callers that need recently active users should request `sort_by=latest_activity&sort_order=desc` so sorting happens before pagination. Course memberships are available via the `user_courses` endpoint below.

The times come from the table the module keeps (the kept activity table, below), not from a read of `user_ips` or `events` at request time, and the answer carries `activity_refreshed_at` (epoch ms, null before the first refresh): a caller that needs the last few minutes calls the refresh endpoint first.

### 2. User Courses (paginated)

`GET /_synapse/client/pangea/v1/user_courses`

- **Auth:** Matrix bearer token, server admin required (403 if not admin)
- **Rate limiting:** None (admin-only, no rate limiting)
- **Required params:** `user_id`
- **Query params:** `page` (int, default 1), `limit` (int, default 50, max 200)

**Response:**

```json
{
  "user_id": "@alice:example.com",
  "docs": [
    {
      "room_id": "!abc:example.com",
      "room_name": "Spanish 101",
      "is_course": true,
      "is_activity": false,
      "activity_id": null,
      "parent_course_room_id": null,
      "most_recent_activity_ts": 1700000000000
    }
  ],
  "page": 1,
  "limit": 50,
  "totalDocs": 12,
  "maxPage": 1
}
```

Courses are sorted by `most_recent_activity_ts` descending — the most recently active course is first. For course rooms, `most_recent_activity_ts` aggregates the user's last message in both the course room itself and its child activity rooms.

### 3. Course Activities (paginated, with user filtering)

`GET /_synapse/client/pangea/v1/course_activities`

- **Auth:** Matrix bearer token, server admin required (403 if not admin)
- **Rate limiting:** None (admin-only, no rate limiting)
- **Required params:** `course_room_id`
- **Query params:** `page` (int, default 1), `limit` (int, default 50, max 200)
- **Optional params (mutually exclusive):**
  - `include_user_id` — only activities where user IS a member
  - `exclude_user_id` — only activities where user is NOT a member

**Response:**

```json
{
  "course_room_id": "!abc:example.com",
  "activities": [
    {
      "room_id": "!def:example.com",
      "room_name": "Activity 1",
      "activity_id": "act-123",
      "members": ["@alice:example.com"],
      "created_ts": 1700000000000
    }
  ],
  "page": 1,
  "limit": 50,
  "totalDocs": 12,
  "maxPage": 1
}
```

Activity rooms sorted by `created_ts` descending — most recent first.

### 4. Refresh the kept activity table

`POST /_synapse/client/pangea/v1/user_activity_refresh`

- **Auth:** Matrix bearer token, server admin required (403 if not admin)
- **Query params:** `full` (`true` rebuilds the table from scratch; default incremental)

**Response:** `{"full": false, "refreshed_at": 1700000000000, "logins_upserted": 12, "messages_upserted": 7, "event_chunks": 1, "duration_ms": 40}`. One refresh runs at a time per process; a second call waits for the first.

## Why Three Endpoints

The original single endpoint embedded courses inside each user doc and activity rooms in a top-level dict. This caused:

- **Scaling issues** — response grew with user count × course count × activity count
- **Redundant data** — activity rooms were duplicated for every request
- **Unbounded course lists** — a user with many courses bloated their user doc

Splitting into three endpoints lets the bot:
1. Paginate through users efficiently (lightweight docs)
2. Fetch courses per-user on demand (paginated, sorted by recency)
3. Fetch course activities per-course with `exclude_user_id` filtering

## DB Query Strategy

### Users queries (`get_users.py`) and the kept activity table (`activity_cache.py`)

**Invariant (Will, 2026-10-21): no request to this route reads `user_ips` or `events` in bulk.** The route used to aggregate the whole `user_ips` table in a CTE on every call and scan `events`, which has no sender index, for the page's last messages: on production that was a minute per call and the database's memory with it, and it took the engagement runner's refresh down with it.

The module keeps one table, `pangea_user_activity` (one row per user: `last_login_ts`, `last_message_ts`, `updated_at_ms`, indexed on both times), and two watermarks. A refresh brings it up to date incrementally: logins from the `user_ips` rows with `last_seen` past the last refresh (the `user_ips_last_seen_only` index), messages from the `m.room.message` events with a `stream_ordering` past the last refresh (the primary key), in chunks of 200,000 orderings so a rebuild is many short transactions. A full refresh empties the table and the watermarks first, so the first refresh after the table is created, and any rebuild, is the one bulk read. Postgres only (`GREATEST`, `ON CONFLICT`), as the route already was.

The refresh runs three ways: on a loop every `user_activity_refresh_interval_seconds` (300 by default; 0 turns the loop off), starting fifteen seconds after the server starts; on demand through the refresh endpoint; and, with `user_activity_refresh_on_read: true`, as a rebuild on every read, which only a small server can afford (the local stack, the test suite).

What this gives up, and why that is enough: the times are as fresh as the last refresh, five minutes by default, and a caller that needs fresher calls the refresh first. The engagement runner reads a person's times during an hourly refresh and before a send, and the deploy traffic check reads a thirty-minute window; neither needs the last few minutes. Backfilled remote history (negative stream orderings) is never folded in, and a message's time edited after the fact is seen only by a full refresh.

1. **Count** — active (non-deactivated, non-guest) users, left-joined to the kept table when `inactive_days` filters on the times.
2. **Page** — one query over `users`, `profiles` and the kept table, sorted by the requested key before `LIMIT/OFFSET`; a user with no row has no recorded activity (0).
3. **Latest activity** — `latest_activity_ts` is always `max(last_login_ts, last_message_ts)`.

### User Courses queries (`get_user_courses.py`)

1. **Memberships** — `SELECT DISTINCT` rooms where user has `join` membership AND room has `pangea.course_plan` or `pangea.activity_plan` state.
2. **State events** — Batch classify rooms as course vs activity.
3. **Room names** — Batch-fetch `m.room.name`.
4. **Space parents** — Map activity rooms → parent course.
5. **Last message per room** — User's last message in each room, bubbled up to parent course for `most_recent_activity_ts`.
6. **Sort + paginate** — In-memory sort by `most_recent_activity_ts` desc, then slice for pagination.

### Course Activities queries (`get_course_activities.py`)

1. **Verify course** — Confirm room has `pangea.course_plan` state.
2. **Find children** — Rooms with `m.space.parent` pointing to this course AND `pangea.activity_plan` state.
3. **Activity IDs, room names, members, creation timestamps** — Batch queries.
4. **Filter, assemble & paginate** — Apply `include_user_id` / `exclude_user_id`, sort by `created_ts` desc, then slice for pagination.

The `user_courses`, `course_activities` and notification-cooldown queries on `events` are scoped by room or sender and timestamp; the users route itself never queries `events` or `user_ips` at request time (the kept activity table, above).

## Bot-Side Consumer

The [engagement doc](../../../pangea-bot/.github/instructions/engagement.instructions.md) in `pangea-bot`:

1. Paginates through all users via `user_activity`
2. For each inactive user, fetches their courses via `user_courses` — picks the first course (most recently active)
3. Calls `course_activities` with `exclude_user_id=<user_id>` to find activity rooms the user hasn't joined
4. Picks the newest eligible room and invites the user

## Key Files

- **Endpoint handlers:** [`user_activity.py`](../../synapse_pangea_chat/user_activity/user_activity.py) — `UserActivity`, `UserCourses`, and `CourseActivities` Resource classes
- **Users query:** [`get_users.py`](../../synapse_pangea_chat/user_activity/get_users.py)
- **The kept activity table and its refresh:** [`activity_cache.py`](../../synapse_pangea_chat/user_activity/activity_cache.py)
- **User courses query:** [`get_user_courses.py`](../../synapse_pangea_chat/user_activity/get_user_courses.py)
- **Course activities query:** [`get_course_activities.py`](../../synapse_pangea_chat/user_activity/get_course_activities.py)
- **Rate limiting:** [`is_rate_limited.py`](../../synapse_pangea_chat/user_activity/is_rate_limited.py)
- **E2E tests:** [`test_user_activity_e2e.py`](../../tests/test_user_activity_e2e.py)

## Future Work

_(No linked issues yet.)_
