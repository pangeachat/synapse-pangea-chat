# Changelog

## Unreleased — Consolidate `public_courses` into sub-package + unify Ansible config

### What changed

#### `synapse-pangea-chat` (code)

Moved `public_courses` from loose top-level files into its own sub-package, matching the pattern used by every other module (`room_preview/`, `room_code/`, `delete_room/`, etc.).

**Deleted files:**
- `synapse_pangea_chat/public_courses.py`
- `synapse_pangea_chat/get_public_courses.py`
- `synapse_pangea_chat/is_rate_limited.py`
- `synapse_pangea_chat/types.py`

**New package — `synapse_pangea_chat/public_courses/`:**
- `__init__.py` — re-exports `PublicCourses`, `_cache`, `get_public_courses`, `RateLimitError`, `is_rate_limited`, `request_log`, `Course`, `PublicCoursesResponse`
- `public_courses.py` — HTTP resource (moved, imports updated)
- `get_public_courses.py` — query logic (moved, imports updated)
- `is_rate_limited.py` — rate limiter (moved, only used by public courses)
- `types.py` — `Course` / `PublicCoursesResponse` TypedDicts (moved)
- `py.typed` — PEP 561 marker

**Test import update — `tests/test_room_preview_e2e.py`:**
- `from synapse_pangea_chat.get_public_courses import _cache` → `from synapse_pangea_chat.public_courses import _cache`
- `from synapse_pangea_chat.is_rate_limited import request_log` → `from synapse_pangea_chat.public_courses import request_log`

**No behavior changes.** The `PangeaChat` class, `PangeaChatConfig`, `parse_config`, and all registered endpoints are identical.

---

#### `pangea-chat-synapse` (Ansible)

Consolidated 6 separate Synapse module entries into the single `synapse_pangea_chat.PangeaChat` module.

**Removed standalone module pip installs:**
- `synapse-room-code`
- `synapse-limit-user-directory`
- `synapse-delete-room-rest-api`
- `synapse-auto-accept-invite-if-knocked`
- `synapse-room-preview`

**Removed standalone module entries from `matrix_synapse_modules`:**
- `synapse_room_code.SynapseRoomCode`
- `synapse_limit_user_directory.SynapseLimitUserDirectory`
- `synapse_delete_room_rest_api.SynapseDeleteRoomRestAPI`
- `synapse_auto_accept_invite_if_knocked.SynapseAutoAcceptInviteIfKnocked`
- `synapse_room_preview.SynapseRoomPreview`

**Config key mapping** (old standalone → centralized):

| Feature | Old key | New key | Value |
|---|---|---|---|
| Room Preview | `requests_per_burst` | `room_preview_requests_per_burst` | 120 |
| Room Preview | `burst_duration_seconds` | `room_preview_burst_duration_seconds` | 120 |
| Room Preview | `room_preview_state_event_types` | `room_preview_state_event_types` | *(unchanged list)* |
| Room Code | `knock_with_code_requests_per_burst` | `knock_with_code_requests_per_burst` | 10 |
| Room Code | `knock_with_code_burst_duration_seconds` | `knock_with_code_burst_duration_seconds` | 60 |
| Delete Room | `delete_room_requests_per_burst` | `delete_room_requests_per_burst` | 120 |
| Delete Room | `delete_room_burst_duration_seconds` | `delete_room_burst_duration_seconds` | 120 |
| Auto-Accept | *(no config)* | `auto_accept_invite_worker` | `None` (default) |
| Limit User Dir | `public_attribute_search_path` | `limit_user_directory_public_attribute_search_path` | `profile.user_settings.public_profile` |
| Limit User Dir | `filter_search_if_missing_public_attribute` | `limit_user_directory_filter_search_if_missing_public_attribute` | `true` |
| Limit User Dir | `whitelist_requester_id_patterns` | `limit_user_directory_whitelist_requester_id_patterns` | `['^@bot:staging.pangea.chat$']` |
| Public Courses | `public_courses_burst_duration_seconds` | `public_courses_burst_duration_seconds` | 120 |
| Public Courses | `public_courses_requests_per_burst` | `public_courses_requests_per_burst` | 120 |
| Public Courses | `course_plan_state_event_type` | `course_plan_state_event_type` | `pangea.course_plan` |

---

### Manual testing checklist

After deploying to staging, verify each feature still works through the unified module:

#### 1. Public Courses — `GET /_synapse/client/unstable/org.pangea/public_courses`
- [ ] Authenticated request returns a list of public courses with `chunk`, `next_batch`, `prev_batch`, `total_room_count_estimate`
- [ ] Each course has: `room_id`, `name`, `topic`, `avatar_url`, `canonical_alias`, `course_id`, `num_joined_members`, `world_readable`, `guest_can_join`, `join_rule`, `room_type`
- [ ] `course_id` matches the `uuid` field from the room's `pangea.course_plan` state event
- [ ] Pagination works (`?limit=N&since=M`)
- [ ] Unauthenticated request returns 401

#### 2. Room Preview — `GET /_synapse/client/unstable/org.pangea/room_preview`
- [ ] `?rooms=!room_id:staging.pangea.chat` returns state events for configured types
- [ ] Response includes `pangea.activity_plan`, `pangea.activity_roles`, `pangea.course_plan`, `m.room.join_rules`, `m.room.name`, `m.room.avatar`, `m.room.power_levels` when present
- [ ] `membership_summary` appears for rooms with activity roles or course plans
- [ ] `m.room.join_rules` content is filtered to only expose `join_rule` key
- [ ] Empty/non-existent room IDs return `{}` (not errors)
- [ ] Unauthenticated request returns 401
- [ ] Cache invalidation works: update a room state event, re-fetch, see updated data

#### 3. Room Code — `POST /_synapse/client/pangea/v1/knock_with_code`
- [ ] Knocking with a valid access code succeeds
- [ ] Knocking with an invalid code is rejected
- [ ] Rate limiting works (10 requests per 60 seconds)

#### 4. Request Room Code — `GET /_synapse/client/pangea/v1/request_room_code`
- [ ] Room admin can request a code for a knock-enabled room
- [ ] Non-admin is rejected

#### 5. Auto-Accept Invite If Knocked
- [ ] User knocks on a room → gets invited → invite is auto-accepted
- [ ] Normal invites (without prior knock) are not affected

#### 6. Delete Room — `POST /_synapse/client/pangea/v1/delete_room`
- [ ] Room creator with highest power level can delete a room
- [ ] Space-child/parent relationships are cleaned up on deletion
- [ ] Rate limiting works (120 requests per 120 seconds)

#### 7. Limit User Directory
- [ ] User directory search filters users without `profile.user_settings.public_profile`
- [ ] Bot user (`@bot:staging.pangea.chat`) bypasses the filter (whitelisted)
- [ ] Users with the public attribute appear in search results normally

#### 8. General
- [ ] Synapse starts without errors in the logs (no `ModuleNotFoundError`, no config parse errors)
- [ ] Only `synapse_pangea_chat` appears in pip packages (no leftover standalone module packages)
- [ ] Verify with: `docker exec matrix-synapse pip list | grep synapse` — should show only `synapse_pangea_chat`
