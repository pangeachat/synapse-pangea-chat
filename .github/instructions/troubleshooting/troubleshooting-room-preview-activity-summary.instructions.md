---
description: "Troubleshooting room_preview responses that omit or stale pangea.activity_summary state."
applyTo: "synapse_pangea_chat/room_preview/**,synapse_pangea_chat/__init__.py,tests/test_room_preview_e2e.py"
---

# Room Preview Activity Summary Troubleshooting

- Symptom: `room_preview` returns missing, null, or stale `pangea.activity_summary` for an activity room, including when requester is a course admin but not an activity-room member.
- Access model: `room_preview` is not per-room-member filtered; any authenticated requester gets the same configured preview state for a room, subject to rate limiting.
- Config check: `pangea.activity_summary` appears only when `room_preview_state_event_types` includes it; do not make it mandatory if deploy config omits it.
- State-key contract: preserve Matrix state keys. Convert only empty state keys to `default`; keep language keys like `en` or `vi`; do not alias non-empty keys to `default`.
- Root cause to check: querying historical `events`/`state_events` and choosing by `origin_server_ts` can select stale state. Fetch preview state from `current_state_events` joined to `event_json`.
- Verification: add or run `tests.test_room_preview_e2e.TestE2E.test_room_preview_activity_summary_current_keys_for_course_admin`; it covers course-admin/non-member parity, multiple summary state keys, and no fabricated `default` key.
