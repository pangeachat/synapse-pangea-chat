---
applyTo: "synapse_pangea_chat/delete_user/**,synapse_pangea_chat/export_user_data/**,synapse_pangea_chat/__init__.py,tests/test_delete_user_e2e.py,tests/test_export_user_data_e2e.py,tests/test_export_user_data_unit.py"
---

# Delete User & Export User Data

Two admin-adjacent Pangea endpoints extend Synapse with controlled account lifecycle operations:

- `/_synapse/client/pangea/v1/delete_user` schedules, cancels, or forces local account deletion.
- `/_synapse/client/pangea/v1/export_user_data` schedules, cancels, forces, or inspects local data export jobs.

## Compatibility Contract

- These endpoints must remain available on Synapse `1.124.0` and newer.
- Compatibility must be gated by the actual capability each endpoint needs, not by broad version checks.
- If a newer Synapse helper is unavailable on older supported versions, prefer a local compatibility shim over disabling the endpoint.
- `COMPAT.yml` must only declare a minimum Synapse version when a real, audited incompatibility remains.

## Request Model

- Both endpoints accept authenticated `POST` requests with JSON bodies.
- `delete_user` supports `schedule`, `cancel`, and `force`.
- `export_user_data` supports `schedule`, `cancel`, `force`, and `status`.
- Omitting `user_id` targets the requester.
- Remote users must be rejected; these flows are only for local accounts.

## Authorization

- Users may operate on their own account/export.
- Targeting another user requires Synapse admin privileges.
- Invalid or missing access tokens must return the same authentication semantics as the rest of the module surface.

## Scheduling Behavior

- Both features maintain durable schedule state in Synapse's database so work survives process restarts.
- Scheduled work is processed asynchronously on a recurring background loop.
- The scheduling mechanism must preserve existing behavior across supported Synapse versions, including short test-time intervals.
- Repeated force runs should be idempotent at the API contract level: callers receive a successful operation rather than a route-level failure when the feature is enabled.

## Side Effects

- `delete_user` must deactivate the Matrix account and remove related CMS feedback-log artifacts when configured.
- `export_user_data` must produce a complete export payload, including CMS feedback logs when available, and degrade gracefully when CMS is unavailable.
- Failures in optional downstream cleanup or upload work must not silently disable the endpoint itself.

## Registration

- `PangeaChat` must register both endpoints whenever their required runtime capabilities are present on supported Synapse versions.
- Logging may explain degraded behavior, but supported versions must not lose the route entirely due to optional helper imports.

## Key Files

- Endpoint registration: [synapse_pangea_chat/__init__.py](../../synapse_pangea_chat/__init__.py)
- Delete user: [synapse_pangea_chat/delete_user/delete_user.py](../../synapse_pangea_chat/delete_user/delete_user.py)
- Export user data: [synapse_pangea_chat/export_user_data/export_user_data.py](../../synapse_pangea_chat/export_user_data/export_user_data.py)
- Config: [synapse_pangea_chat/config.py](../../synapse_pangea_chat/config.py)
- Tests: [tests/test_delete_user_e2e.py](../../tests/test_delete_user_e2e.py), [tests/test_export_user_data_e2e.py](../../tests/test_export_user_data_e2e.py), [tests/test_export_user_data_unit.py](../../tests/test_export_user_data_unit.py)

## Future Work

_Last updated: 2026-03-20_

**Privacy & Compliance**

- [pangeachat/security#26](https://github.com/pangeachat/security/issues/26) — Build JSON data export tooling
- [pangeachat/security#47](https://github.com/pangeachat/security/issues/47) — W40: Client does not pass erase:true on account deactivation
