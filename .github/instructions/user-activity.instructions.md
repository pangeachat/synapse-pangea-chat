---
applyTo: "synapse_pangea_chat/user_activity/**"
---

# User Activity Endpoints

Three Synapse module endpoints that power the re-engagement system. Called by the [bot engagement script](../../../bot/.github/instructions/initiate.engagement.instructions.md).

## Endpoints

### 1. Users (paginated)

`GET /_synapse/client/pangea/v1/user_activity`

- **Auth:** Matrix bearer token, server admin required (403 if not admin)
- **Rate limiting:** None (admin-only, no rate limiting)
- **Query params:** `page` (int, default 1), `limit` (int, default 50, max 200)

**Response:**

```json
{
  "docs": [
    {
      "user_id": "@alice:example.com",
      "display_name": "Alice",
      "last_login_ts": 1700000000000,
      "last_message_ts": 1700000000000
    }
  ],
  "page": 1,
  "limit": 50,
  "totalDocs": 200,
  "maxPage": 4
}
```

User docs contain only basic activity metadata. Course memberships are available via the `user_courses` endpoint below.

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

### Users queries (`get_users.py`)

1. **Count** — Total active (non-deactivated, non-guest) users.
2. **Users + last login** — CTE pre-aggregates `user_ips` then JOINs, `LIMIT/OFFSET` for pagination.
3. **Last message per user** — `events` table scoped with `WHERE sender IN (...)`.

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

All `events` table queries are scoped to specific user IDs via `WHERE sender IN (...)` or `WHERE sender = ?` to prevent full-table scans.

## Bot-Side Consumer

The [engagement script](../../../bot/.github/instructions/initiate.engagement.instructions.md) in `pangea-bot`:

1. Paginates through all users via `user_activity`
2. For each inactive user, fetches their courses via `user_courses` — picks the first course (most recently active)
3. Calls `course_activities` with `exclude_user_id=<user_id>` to find activity rooms the user hasn't joined
4. Picks the newest eligible room and invites the user

## Key Files

- **Endpoint handlers:** [`user_activity.py`](../../synapse_pangea_chat/user_activity/user_activity.py) — `UserActivity`, `UserCourses`, and `CourseActivities` Resource classes
- **Users query:** [`get_users.py`](../../synapse_pangea_chat/user_activity/get_users.py)
- **User courses query:** [`get_user_courses.py`](../../synapse_pangea_chat/user_activity/get_user_courses.py)
- **Course activities query:** [`get_course_activities.py`](../../synapse_pangea_chat/user_activity/get_course_activities.py)
- **Rate limiting:** [`is_rate_limited.py`](../../synapse_pangea_chat/user_activity/is_rate_limited.py)
- **E2E tests:** [`test_user_activity_e2e.py`](../../tests/test_user_activity_e2e.py)

## Future Work

_(No linked issues yet.)_
