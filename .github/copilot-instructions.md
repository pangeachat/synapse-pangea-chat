Check the relevant `.github/instructions/` doc before and after coding. If it doesn't exist, create it with the user first. Follow [instructions-authoring.instructions.md](../../.github/instructions/instructions-authoring.instructions.md) for doc standards.

# synapse-pangea-chat

Unified Synapse module (Python 3.10+) bundling all Pangea Chat server-side features into `synapse_pangea_chat.PangeaChat`.

## Architecture

Single entry-point class `PangeaChat` (`synapse_pangea_chat/__init__.py`) composes seven sub-modules. Each sub-module lives in its own sub-package under `synapse_pangea_chat/`.

| Sub-module           | Package                 | Endpoints                                                                                                                                         |
| -------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Public Courses       | `public_courses/`       | `GET /_synapse/client/unstable/org.pangea/public_courses`                                                                                         |
| Room Preview         | `room_preview/`         | `GET /_synapse/client/unstable/org.pangea/room_preview`                                                                                           |
| Room Code            | `room_code/`            | `POST /_synapse/client/pangea/v1/knock_with_code`, `GET /_synapse/client/pangea/v1/request_room_code`                                             |
| Request Auto Join    | `request_auto_join/`    | `POST /_synapse/client/unstable/org.pangea/v1/request_auto_join`                                                                                  |
| Auto Accept Invite   | `auto_accept_invite/`   | _(third-party rules callback — no HTTP endpoint)_                                                                                                 |
| Delete Room          | `delete_room/`          | `POST /_synapse/client/pangea/v1/delete_room`                                                                                                     |
| User Activity        | `user_activity/`        | `GET /_synapse/client/pangea/v1/user_activity`, `GET /_synapse/client/pangea/v1/user_courses`, `GET /_synapse/client/pangea/v1/course_activities` |
| Limit User Directory | `limit_user_directory/` | _(spam checker callback — no HTTP endpoint)_                                                                                                      |

## Key Files

- **Entry point**: `synapse_pangea_chat/__init__.py` — `PangeaChat` class, registers all resources & callbacks
- **Config**: `synapse_pangea_chat/config.py` — `PangeaChatConfig` (attrs, all keys optional with defaults)
- **Config parsing**: `PangeaChat.parse_config()` in `__init__.py`

## Conventions

- Python **≥ 3.10** (no 3.8/3.9 support)
- Linting: `black`, `ruff` — run `tox -e check_codestyle` before committing
- Type checking: `mypy`
- Runtime dependency: `attrs`
- Testing & code style details: see [testing.instructions.md](.github/instructions/testing.instructions.md)
