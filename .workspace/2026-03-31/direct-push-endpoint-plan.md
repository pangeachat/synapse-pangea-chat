# Plan: Direct Push Endpoint on Synapse Module

- Date: 2026-03-31
- Repo: `synapse-pangea-chat-modules`
- Scope decided here:
  - Add an admin-only HTTP endpoint in the Synapse module for immediate push delivery without creating any Matrix event.
  - Treat this as a Synapse-module and Sygnal delivery design, with only a short client-implications section.
  - Assume the endpoint will authenticate using a Synapse admin access token.
  - Assume production support is required, not staging-only.
  - Prefer a design that is robust to dedicated pusher workers in production.

## Summary

The existing event-driven bot notification path is not a good fit for the new goal.

- The bot currently sends a real `p.room.notice` event and relies on client push rules to notify.
- That path also performs translation and tokenization work before the event send, which adds latency unrelated to actual push delivery.
- The new requirement is lower-level and simpler: send a push immediately, over HTTP, with no backing Matrix event.

Synapse exposes two viable implementation directions:

1. Use `ModuleApi.send_http_push_notification(...)`.
2. Read the user's registered HTTP pushers from Synapse storage and POST the same payload shape directly to each pusher's registered push gateway URL.

The first option is thinner but depends on in-memory pushers on the current process. Because production runs dedicated pusher workers, that creates avoidable topology risk. The second option is slightly more custom but works against the persisted pusher registrations and is the safer default for production.

## Desired Outcome

- A new admin-only endpoint exists on the Synapse module.
- Callers can trigger immediate push delivery to one user, with optional device scoping.
- The endpoint does not create Matrix events or rely on push-rule evaluation.
- The endpoint is resilient to Synapse worker topology differences between staging and production.
- The response clearly reports per-device delivery attempt results.

## Recommended Approach

Implement a new Synapse-module endpoint that:

- Authenticates the request with `ModuleApi.get_user_by_req(...)`.
- Verifies the requester is a server admin with `ModuleApi.is_user_admin(...)`.
- Loads the target user's registered HTTP pushers from Synapse storage.
- Filters to enabled HTTP pushers, optionally by `device_id`.
- Builds the same `notification.devices[0]` payload shape used by Synapse's `HttpPusher.dispatch_push(...)`.
- POSTs that payload directly to each pusher's registered push gateway URL.
- Returns per-device status, plus any rejected pushkeys or transport errors.

This keeps the endpoint aligned with Synapse's existing push payload contract while avoiding dependence on where active pusher objects live in memory.

## Proposed Endpoint Contract

### Route

`POST /_synapse/client/pangea/v1/send_push`

### Auth

- Matrix access token required.
- Requester must be a Synapse server admin.
- Non-admin callers get `403`.

### Request Shape

Proposed default schema:

```json
{
  "user_id": "@alice:pangea.chat",
  "device_id": "ABC123",
  "room_id": "!room:pangea.chat",
  "event_id": "push-20260331-abc123",
  "body": "Your practice partner is waiting.",
  "title": "Pangea Chat",
  "type": "pangea.direct_push",
  "content": {
    "check_in_type": "default",
    "pangea.activity.session_room_id": "!session:pangea.chat",
    "pangea.activity.id": "activity-123"
  },
  "counts": {
    "unread": 1
  },
  "prio": "high"
}
```

### Notes on Fields

- `user_id`: required.
- `device_id`: optional. If omitted, fan out to all enabled HTTP pushers for that user.
- `room_id`: recommended required field so notification taps still have a routing target.
- `event_id`: synthetic identifier, not a real Matrix event ID. This preserves compatibility with current notification-tap code paths that expect a string `event_id` in the payload.
- `body`: required visible notification body.
- `title`: optional visible notification title.
- `type`: optional logical type for downstream payload consumers.
- `content`: arbitrary extra content flattened or preserved for client consumption.
- `counts`: optional unread-count section.
- `prio`: optional, default `high`.

### Response Shape

```json
{
  "user_id": "@alice:pangea.chat",
  "attempted": 2,
  "sent": 2,
  "failed": 0,
  "devices": {
    "ABC123": {
      "sent": true,
      "app_id": "com.talktolearn.chat.data_message",
      "pushkey": "<redacted>",
      "url": "https://sygnal.pangea.chat/_matrix/push/v1/notify"
    },
    "XYZ999": {
      "sent": true,
      "app_id": "com.talktolearn.chat",
      "pushkey": "<redacted>",
      "url": "https://sygnal.pangea.chat/_matrix/push/v1/notify"
    }
  },
  "errors": []
}
```

## Why Not Use `ModuleApi.send_http_push_notification(...)`

It is a real option and is suitable for a simpler staging-only or single-process pusher setup.

However:

- It iterates active in-memory HTTP pushers on the current process.
- Staging runs pushers on the main process, so it likely works there.
- Production uses a dedicated pusher worker, so the main-process module endpoint may not see the relevant pusher objects.

Unless the endpoint is explicitly hosted where the pusher workers live, or additional replication is introduced, the storage-driven direct POST design is the safer production plan.

## Checklist

### 1. Confirm payload and targeting contract

- [ ] Confirm whether `device_id` remains optional with default fanout to all devices.
- [ ] Confirm whether `room_id` should be required.
- [ ] Confirm whether the endpoint should require a caller-provided synthetic `event_id` or generate one server-side.
- [ ] Confirm whether arbitrary extra payload should live under `content` only, or whether raw custom top-level fields are allowed.
- [ ] Confirm whether unread counts are caller-controlled or omitted entirely.

### 2. Add the module endpoint

- [ ] Add a new resource class, likely under a new subpackage such as `synapse_pangea_chat/direct_push/`.
- [ ] Register the route from `synapse_pangea_chat/__init__.py`.
- [ ] Reuse the existing resource style used by `invite_by_email`, `delete_room`, and `user_activity`.
- [ ] Authenticate with `ModuleApi.get_user_by_req(...)`.
- [ ] Reject non-admin callers using `ModuleApi.is_user_admin(...)`.

### 3. Implement storage-driven pusher fanout

- [ ] Read the target user's pushers from Synapse storage.
- [ ] Filter to enabled `kind == "http"` pushers only.
- [ ] Optionally filter by `device_id` if provided.
- [ ] Ignore non-HTTP pushers entirely.
- [ ] Build per-pusher `devices` payload entries with the pusher's `app_id`, `pushkey`, `pushkey_ts`, and `data`.
- [ ] Preserve pusher `data` fields such as the client's `data_message` settings.
- [ ] POST the payload to the pusher's registered URL using Synapse's HTTP client.

### 4. Define error handling and response semantics

- [ ] Return `404` if the target user does not exist.
- [ ] Return `404` or `200` with zero attempts if the user has no enabled HTTP pushers. Decide explicitly.
- [ ] Return per-device success and failure status.
- [ ] Redact or partially redact pushkeys in the response.
- [ ] Log failures with enough context for debugging while avoiding leaking raw pushkeys unnecessarily.
- [ ] Capture transport or gateway errors to Sentry.

### 5. Validate client compatibility assumptions

- [ ] Confirm the client can handle a synthetic `event_id` in the notification payload.
- [ ] Confirm notification taps still route correctly when there is no backing Matrix event.
- [ ] Confirm `p.room.notice.opened` behavior is either intentionally skipped or updated for the no-event flow.
- [ ] Decide whether this endpoint should be used only for pushes that do not need opened-event analytics.

### 6. Testing

- [ ] Add unit or integration tests for admin auth.
- [ ] Add tests for fanout to all devices.
- [ ] Add tests for single-device targeting.
- [ ] Add tests for users with no pushers.
- [ ] Add tests for mixed pushers where only HTTP pushers are used.
- [ ] Add tests for gateway error handling and partial failures.
- [ ] If feasible, add a local integration test using a fake push endpoint instead of real Sygnal.

### 7. Staging validation

- [ ] Deploy to staging.
- [ ] Use an admin access token to call the endpoint against a known user/device with active pushers.
- [ ] Verify notification arrives without any Matrix room event being created.
- [ ] Verify multi-device fanout behavior if the test user has multiple pushers.
- [ ] Verify failure behavior for a user with no pushers.

### 8. Production rollout

- [ ] Confirm the route and auth model are acceptable for production use.
- [ ] Confirm whether any operational rate limit is needed for admin callers.
- [ ] Deploy module update to staging first, then production.
- [ ] Smoke-test with one internal account before broader use.

## Code Touchpoints

- `synapse_pangea_chat/__init__.py`
  - Register the new endpoint.
- `synapse_pangea_chat/config.py`
  - Add any config values if rate limits or payload constraints become configurable.
- New module files, likely under `synapse_pangea_chat/direct_push/`
  - Request parsing.
  - Admin auth.
  - Pusher lookup and fanout.
  - Payload construction.
- `tests/`
  - Endpoint-level tests.

## Client Implications

The endpoint intentionally breaks the old assumption that every push corresponds to a real Matrix event.

That means:

- notification tap behavior must tolerate synthetic `event_id` values,
- opened-event analytics may need to be skipped or redesigned for this flow,
- any feature that tries to fetch the notification event by ID will not work unless the client is adjusted not to rely on that.

This plan assumes that contract change is acceptable.

## Open Decisions To Resolve During Implementation

- [ ] Whether `event_id` is required from the caller or generated by the module.
- [ ] Whether `room_id` is required or merely recommended.
- [ ] Whether the endpoint response should expose the destination Sygnal URL per device or keep that internal.
- [ ] Whether users with no active HTTP pushers should be treated as `404`, `409`, or `200` with zero attempts.
- [ ] Whether to add explicit rate limiting even for admin-only access.

## Notes

- The production-safe default is storage-driven fanout, not in-memory pusher fanout.
- The endpoint should stay intentionally narrow: admin-only, HTTP push only, no attempt to emulate full Matrix event semantics.
- If later needed, a second endpoint can be added for a richer payload builder once real call sites settle.