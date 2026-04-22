---
applyTo: "synapse_pangea_chat/direct_push/**,synapse_pangea_chat/config.py,synapse_pangea_chat/__init__.py,tests/test_direct_push*.py"
---

# Direct Push — Synapse Module

Admin-only endpoint for sending a roomless push notification to a local user.

For Synapse Admin API, Module API, and Matrix spec documentation links, see [synapse-docs.instructions.md](../../../.github/.github/instructions/synapse-docs.instructions.md).

## Endpoint

`POST /_synapse/client/pangea/v1/send_push`

Lives in the `direct_push/` sub-package.

## Contract

- **Auth**: Server admin only.
- **Request**: Requires `user_id` and `body`.
- **Optional request fields**: `device_id`, `room_id`, `event_id`, `type`, `content`, and `prio`.
- **Response**: Returns delivery attempts and errors for the transport path the endpoint invoked itself. It does not imply that a Matrix event was persisted or that every Synapse pusher kind was exercised.

## Behavior

- Works below the Matrix event layer.
- Does not forge or persist a Matrix event.
- Does not create timeline entries, unread state, receipts, or `event_push_actions`.
- This roomless behavior is intentional: callers can send a push notification without attaching it to any room.
- The current implementation delivers through its own lower-level transport path rather than delegating to Synapse's normal event-driven notification pipeline.

## Current Limitation

- The endpoint is roomless by design, but that also means it is not truly pusher-agnostic in practice.
- Pushers that depend on Matrix event persistence are not triggered by default.
- In particular, normal email notification delivery is not produced automatically by this endpoint.

## Intended Use

- Operational or bot-driven nudges where roomless delivery is required.
- Transporting a push payload to a user who already has compatible pushers configured on the homeserver.

## Non-Goals

- Creating a canonical chat or room-history record of the notification.
- Reusing room notification semantics such as unread counts or reopen tracking.
- Guaranteeing identical behavior across every configured pusher kind.

## Key Files

- [`direct_push.py`](../../synapse_pangea_chat/direct_push/direct_push.py) — admin-only roomless push endpoint
- [`__init__.py`](../../synapse_pangea_chat/__init__.py) — resource registration
- [`config.py`](../../synapse_pangea_chat/config.py) — direct-push config fields
- [`test_direct_push_e2e.py`](../../tests/test_direct_push_e2e.py) — endpoint behavior coverage

## Future Work

_Last updated: 2026-04-22_

**Endpoint semantics**

- [pangeachat/synapse-pangea-chat#79](https://github.com/pangeachat/synapse-pangea-chat/issues/79) — Add optional email delivery flag to direct-push endpoint
