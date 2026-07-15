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
- Delete retries are bounded, never infinite. Each delete schedule persists an attempt counter; after a small fixed number of failed attempts (5) the schedule is dropped as terminally failed with one final error log naming the user and the terminal reason. Failures that can never succeed on retry (a 404: the user row is already gone) terminate the schedule on the first attempt. A terminally failed schedule holds no state — a fresh `schedule` or `force` request starts over cleanly.

## Side Effects

- `delete_user` deactivates the Matrix account. The deletion does not reach CMS user data today; the erase cascade below is the durable design that closes that gap.
- `export_user_data` produces a complete export payload from Synapse-side data and degrades gracefully when CMS is unavailable.
- Failures in optional downstream cleanup or upload work must not silently disable the endpoint itself.

## CMS Erase Cascade (PII on deletion)

On account deletion, a CMS-native erase cascade removes the user's PII held in handler collections: it deletes the rating-comment text and redacts other user-authored text in those collections. The erase step is idempotent and decoupled from — and ordered before — the irreversible Synapse deactivation, so a CMS outage can't wedge the deletion retry loop while PII still lingers (a wedged retry against a dead CMS would otherwise leave PII undeleted with no path forward). The erase endpoint lives in CMS; this module calls it.

Export reciprocally includes the user's rating-ledger rows. The generated export artifact has a 30-day retention and is cascade-deleted when the user is deleted, so a stale export can't outlive the account it describes.

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
