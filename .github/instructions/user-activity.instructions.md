---
applyTo: "synapse_pangea_chat/user_engagement/**"
---

# User Engagement Endpoint

Admin-only endpoint that returns per-user engagement data from the Synapse PostgreSQL database. Consumed by the bot's [engagement script](../../../pangea-bot/.github/instructions/initiate.engagement.instructions.md) and individually by engagement actions like [invite-to-activity](../../../pangea-bot/.github/instructions/invite-to-activity.engagement.instructions.md).

## Endpoint Contract

`GET /_synapse/client/pangea/v1/user_engagement`

- **Auth:** Bearer token, server admin only (403 for non-admin, 401 for missing/invalid token)
- **Rate limit:** Configurable via `user_engagement_requests_per_burst` (default 10) and `user_engagement_burst_duration_seconds` (default 60) in the module config

### Query Parameters

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `since_days` | int | 7 | Timestamp floor for `events`-table queries. Only affects `last_message_ts`. `last_login_ts` (from `user_ips`) is **always** the true value regardless of this param. Hard-capped at **90 days** server-side — any value > 90 is silently clamped. |

> ⚠️ **Increasing `since_days` is dangerous.** Large windows scan more of the `events` table and can degrade DB performance. The 7-day default is intentionally tight. Only override when you have a specific reason (e.g., one-off audit) and never in automated/cron usage.

**Interpretation by the caller:** When `last_message_ts` is `null`, it means "no message within the `since_days` window" — not "user has never sent a message." `last_login_ts` is the reliable inactivity signal because it's unaffected by the floor.

### Response Shape

```json
{
  "users": [
    {
      "user_id": "@user:domain",
      "display_name": "...",
      "last_login_ts": 1700000000000,
      "last_message_ts": 1700000000000,
      "course_space_ids": ["!room1:domain", "!room2:domain"]
    }
  ]
}
```

| Field | Source | Notes |
|-------|--------|-------|
| `last_login_ts` | `user_ips` | Always the true value — never scoped by `since_days` |
| `last_message_ts` | `events` (type `m.room.message`) | Scoped by `since_days`. `null` = no message in window |
| `course_space_ids` | `room_memberships` + `current_state_events` | Rooms the user has joined that have a `pangea.course_plan` state event. The caller uses these to query `room_preview` for activity session state. |

## DB Query Strategy

The endpoint runs sequential read-only queries against the Synapse Postgres DB via `room_store.db_pool.execute(...)`.

### Query Stages

1. **Users** — `users` + `profiles` + `user_ips` (use CTE/JOIN for last login, not correlated subquery)
2. **Last message per user** — `events` filtered to `m.room.message`, grouped by sender. **Must scope** with `WHERE sender IN (...)` and the `since_days` timestamp floor
3. **Course memberships** — `room_memberships` joined with `current_state_events` to find rooms with `pangea.course_plan` state, yielding `course_space_ids`

### Performance Constraints

- **Never scan the full `events` table.** Always filter by `sender IN (...)` **and** the `since_days` timestamp floor.
- **`user_ips` is always unscoped** — it's a small table and returning the true `last_login_ts` is critical for inactivity detection.
- **Hard cap at 90 days** — the endpoint clamps `since_days` server-side to prevent callers from accidentally scanning months of events.
- **Use CTEs or JOINs** instead of correlated subqueries (e.g., `user_ips` last login).
- **Check indexes** — queries rely on indexes on `events(type, sender)`, `current_state_events(room_id, type)`, and `room_memberships(room_id, membership)`. Verify these exist on production Synapse before deploying.
- The endpoint is **rate-limited** but still runs heavy queries. Do not add caching until we've measured real production load.

## Sub-module Pattern

Follows the existing Twisted Resource pattern:

- `user_engagement.py` — Resource class, request handling, auth, rate limiting
- `get_user_engagement.py` — DB queries and data assembly
- `is_rate_limited.py` — In-memory rate limiter (same pattern as other sub-modules)

Accesses `self._api._hs.get_datastores().main` (private Synapse API — may break on Synapse upgrades, but consistent with all other sub-modules in this repo).

## Future Work

_(No linked issues yet.)_
