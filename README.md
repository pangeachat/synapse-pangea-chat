# Pangea Chat Module for Synapse

Unified [Synapse](https://github.com/element-hq/synapse) module that bundles all Pangea Chat server-side features into a single installable package.

## Modules

| Module | Subpackage | Endpoint | Description |
|--------|-----------|----------|-------------|
| [Public Courses](#public-courses) | `synapse_pangea_chat/` | `GET /_synapse/client/unstable/org.pangea/public_courses` | Course catalog with filtering and pagination |
| [Room Preview](#room-preview) | `synapse_pangea_chat/room_preview/` | `GET /_synapse/client/unstable/org.pangea/room_preview` | Read room state events without membership |
| [Room Code](#room-code) | `synapse_pangea_chat/room_code/` | `POST /_synapse/client/pangea/v1/knock_with_code` | Secret-code-based room invitations |
| | | `GET /_synapse/client/pangea/v1/request_room_code` | Generate a unique room access code |
| [Auto Accept Invite](#auto-accept-invite) | `synapse_pangea_chat/auto_accept_invite/` | *(callback)* | Auto-accept invites for users who previously knocked |
| [Delete Room](#delete-room) | `synapse_pangea_chat/delete_room/` | `POST /_synapse/client/pangea/v1/delete_room` | Room deletion for highest-power-level members |
| [Limit User Directory](#limit-user-directory) | `synapse_pangea_chat/limit_user_directory/` | *(spam checker)* | Filter user directory by public profile attribute |

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
      public_courses_burst_duration_seconds: 120   # default: 120
      public_courses_requests_per_burst: 120       # default: 120
      course_plan_state_event_type: "pangea.course_plan"  # default: null (falls back to pangea.course_plan)

      # --- Room Preview ---
      room_preview_state_event_types:              # additional state event types to expose
        - "p.room_summary"
        - "pangea.activity_plan"
        - "pangea.activity_roles"
      room_preview_burst_duration_seconds: 60      # default: 60
      room_preview_requests_per_burst: 10          # default: 10

      # --- Room Code ---
      knock_with_code_requests_per_burst: 10       # default: 10
      knock_with_code_burst_duration_seconds: 60   # default: 60

      # --- Auto Accept Invite ---
      auto_accept_invite_worker: null              # worker name, null for main process

      # --- Delete Room ---
      delete_room_requests_per_burst: 10           # default: 10
      delete_room_burst_duration_seconds: 60       # default: 60

      # --- Limit User Directory (disabled when path is null) ---
      limit_user_directory_public_attribute_search_path: "profile.user_settings.public"
      limit_user_directory_filter_search_if_missing_public_attribute: true
      limit_user_directory_whitelist_requester_id_patterns:
        - "^@admin:example.com$"
```

All config keys are optional and have sensible defaults. The `limit_user_directory` spam checker is only activated when `limit_user_directory_public_attribute_search_path` is set.

---

## Public Courses

Surface curated course previews via a dedicated HTTP endpoint with built-in filtering and rate limiting.

**Route:** `GET /_synapse/client/unstable/org.pangea/public_courses`

### Authentication

Requires a valid Matrix access token; unauthenticated calls return `401 M_UNAUTHORIZED`. A simple in-memory burst rate limit protects the resource (default: 120 requests per user in a 120-second window, returns `429` when exceeded).

### Query Parameters

| Name    | Type    | Default | Description |
|---------|---------|---------|-------------|
| `limit` | integer | `10`    | Maximum number of courses to return |
| `since` | string  | `None`  | Pagination token returned by a previous call |

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
      "room_type": null
    }
  ],
  "next_batch": "10",
  "prev_batch": null,
  "total_room_count_estimate": 23
}
```

### Room Selection Criteria

Returns only rooms where **all** of the following are true:

1. `rooms.is_public` is true.
2. The room emits the required course plan state event (default `pangea.course_plan`).
3. The endpoint can fetch the latest preview state events in a single consolidated query.

---

## Room Preview

Allow authenticated users to read content of pre-configured state events from rooms without being a member.

**Route:** `GET /_synapse/client/unstable/org.pangea/room_preview`

### Query Parameters

| Name    | Type   | Default | Description |
|---------|--------|---------|-------------|
| `rooms` | string | *(empty)* | Comma-delimited list of room IDs |

### Response

```json
{
  "rooms": {
    "!room_id:example.com": {
      "event_type": {
        "state_key": { /* full event JSON content */ }
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

*Originally: [pangeachat/synapse-room-preview](https://github.com/pangeachat/synapse-room-preview)*

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

*Originally: [pangeachat/synapse-room-code](https://github.com/pangeachat/synapse-room-code)*

---

## Auto Accept Invite

Automatically accept invites for users who previously knocked on a room. When a user knocks on a room and is later invited, the module auto-accepts the invite on their behalf.

- Worker-aware: can be pinned to a specific worker via `auto_accept_invite_worker`.
- Marks DMs appropriately.
- Retries with exponential backoff on failure.

*Originally: [pangeachat/synapse-auto-accept-invite-if-knocked](https://github.com/pangeachat/synapse-auto-accept-invite-if-knocked)*
*Reference: [matrix-org/synapse-auto-accept-invite](https://github.com/matrix-org/synapse-auto-accept-invite)*

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

*Originally: [pangeachat/synapse-delete-room-rest-api](https://github.com/pangeachat/synapse-delete-room-rest-api)*

---

## Limit User Directory

Spam checker callback that filters the user directory based on a public profile attribute. Only users whose profile contains the configured attribute (at the dot-syntax path) set to `true` are visible in search results.

### Config

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `limit_user_directory_public_attribute_search_path` | string \| null | `null` | Dot-syntax path to the public boolean attribute (e.g. `profile.user_settings.public`). Module is disabled when `null`. |
| `limit_user_directory_filter_search_if_missing_public_attribute` | bool | `true` | Whether to filter users who lack the attribute entirely |
| `limit_user_directory_whitelist_requester_id_patterns` | list[str] | `[]` | Regex patterns for user IDs that bypass filtering |

Users sharing a room with the requester are always visible regardless of the public attribute.

*Originally: [pangeachat/synapse-limit-user-directory](https://github.com/pangeachat/synapse-limit-user-directory)*

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
├── auto_accept_invite/          # Auto accept invite module
├── delete_room/                 # Delete room module
└── limit_user_directory/        # Limit user directory module
tests/
├── __init__.py                  # Shared test helpers
├── test_e2e.py                  # Public courses E2E tests
├── test_room_preview_e2e.py     # Room preview E2E tests
├── test_room_preview_reactive_cache.py
├── test_room_code_e2e.py        # Room code E2E tests
├── test_auto_accept_invite_e2e.py
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

 1. Set a shell variable to the version you are releasing (this just makes
    subsequent steps easier):
    ```shell
    version=X.Y.Z
    ```

 2. Update `setup.cfg` so that the `version` is correct.

 3. Stage the changed files and commit.
    ```shell
    git add -u
    git commit -m v$version -n
    ```

 4. Push your changes.
    ```shell
    git push
    ```

 5. When ready, create a signed tag for the release:
    ```shell
    git tag -s v$version
    ```
    Base the tag message on the changelog.

 6. Push the tag.
    ```shell
    git push origin tag v$version
    ```

 7. If applicable:
    Create a *release*, based on the tag you just pushed, on GitHub or GitLab.

 8. If applicable:
    Create a source distribution and upload it to PyPI:
    ```shell
    python -m build
    twine upload dist/synapse_room_preview-$version*
    ```
