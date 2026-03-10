# synapse-pangea-chat

Unified Synapse module (Python 3.10+) bundling all Pangea Chat server-side features into `synapse_pangea_chat.PangeaChat`.

For Synapse Admin API, Module API, and Matrix spec documentation links, see [synapse-docs.instructions.md](../../.github/.github/instructions/synapse-docs.instructions.md).

## Architecture

Single entry-point class `PangeaChat` (`synapse_pangea_chat/__init__.py`) composes sub-modules. Each sub-module lives in its own sub-package under `synapse_pangea_chat/`.

**Sub-modules**: `public_courses/`, `room_preview/`, `room_code/` ([design](.github/instructions/knock-with-code.instructions.md)), `delete_room/`, `user_activity/` ([design](.github/instructions/user-activity.instructions.md)), `limit_user_directory/` + `user_directory_search/` ([design](.github/instructions/limit-user-directory.instructions.md))

## Key Files

- **Entry point**: `synapse_pangea_chat/__init__.py` — `PangeaChat` class, registers all resources & callbacks
- **Config**: `synapse_pangea_chat/config.py` — `PangeaChatConfig` (attrs, all keys optional with defaults)
- **Config parsing**: `PangeaChat.parse_config()` in `__init__.py`
