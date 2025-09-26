# Pangea Chat Module for Synapse

Surface curated course previews from a Synapse homeserver via a dedicated HTTP endpoint with built-in filtering and rate limiting.


## Overview

This module registers a custom resource with Synapse that publishes course metadata to clients. It:

- Exposes a JSON API at `/_synapse/client/unstable/org.pangea/public_courses`.
- Filters to **public** Matrix rooms that advertise the required course state event (defaults to `pangea.course_plan`).
- Fetches the full preview in a single database round trip and caches responses briefly for repeat requests.
- Applies a lightweight request-burst rate limit per authenticated user.

The endpoint is designed to power Pangea's course catalog UI, but any client can consume it once authenticated.


## Public courses endpoint

**Route:** `GET /_synapse/client/unstable/org.pangea/public_courses`

### Authentication

- Requires a valid Matrix access token; unauthenticated calls return `401 M_UNAUTHORIZED`.
- A simple in-memory burst rate limit protects the resource. The default allows 120 requests per user in a 120-second window and returns `429` when exceeded.

### Query parameters

| Name   | Type | Default | Description |
| ------ | ---- | ------- | ----------- |
| `limit` | integer | `10` | Maximum number of courses to return. |
| `since` | string | `None` | Pagination token returned by a previous call. Treated as an integer offset. |

### Response shape

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

- `chunk` contains one entry per qualifying room. Fields that rely on additional Synapse state (`num_joined_members`, `join_rule`, etc.) are currently stubbed and may require client-side enrichment.
- `next_batch` and `prev_batch` are integer cursor strings suitable for the `since` parameter.
- `total_room_count_estimate` reflects the number of rooms evaluated after filtering.

### Room selection criteria

The module returns only rooms that meet **all** of the following conditions:

1. `rooms.is_public` is true.
2. The room emits the required course plan state event (default `pangea.course_plan`).
3. The endpoint can fetch the latest copy of each preview state event (`m.room.name`, `m.room.topic`, `m.room.avatar`, etc.) in the single consolidated query.

The required course event type can be overridden with `course_plan_state_event_type` in the module configuration. Regardless of overrides, the default event is always fetched to keep payload compatibility.


## Installation


## Installation

From the virtual environment that you use for Synapse, install this module with:
```shell
pip install path/to/synapse-pangea-chat
```
(If you run into issues, you may need to upgrade `pip` first, e.g. by running
`pip install --upgrade pip`)

Then alter your homeserver configuration, adding to your `modules` configuration:
```yaml
modules:
  - module: synapse_pangea_chat.PangeaChat
    config:
         public_courses_burst_duration_seconds: 120
         public_courses_requests_per_burst: 120
         course_plan_state_event_type: "pangea.course_plan"
```

Leave `course_plan_state_event_type` unset (or `null`) to fall back to the default `pangea.course_plan` state event. Adjust the burst settings to tune per-user request throttling.

Once loaded, the new endpoint is available immediately from your homeserver.
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
    twine upload dist/synapse_pangea_chat-$version*
    ```
