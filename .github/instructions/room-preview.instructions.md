---
description: "Room preview endpoint contract for configured state events, response shape, current-state reads, and cache-safe behavior."
applyTo: "synapse_pangea_chat/room_preview/**,synapse_pangea_chat/__init__.py,tests/test_room_preview_e2e.py"
---

# Room Preview

- `room_preview_state_event_types` is config-driven. Do not expose optional state event types such as `pangea.activity_summary` when deploy config omits them.
- Response shape is `room_id -> event_type -> state_key -> full event JSON`. Convert only empty Matrix state keys to `default`; preserve non-empty keys such as language codes.
- Return all current state keys for each configured event type. Do not select a preferred language, collapse keys, or alias non-empty keys to `default`.
- Fetch preview state from `current_state_events` joined to `event_json`, not historical `events`/`state_events` ordered by timestamp.
- Keep `m.room.join_rules` filtering narrow: only `content.join_rule` is exposed; privacy-sensitive fields such as `allow` stay stripped.
