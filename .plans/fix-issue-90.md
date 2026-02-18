# Fix github issue #90

- Original issue: https://github.com/pangeachat/synapse/issues/90
- Related issue: https://github.com/pangeachat/synapse/issues/84
- Failed fix PR: https://github.com/pangeachat/synapse-pangea-chat/pull/9
- Crash caused by fix: https://github.com/pangeachat/synapse-pangea-chat/issues/11
- Revert PR: https://github.com/pangeachat/synapse-pangea-chat/pull/13
- Decommissioned fix branch: `fix/11-auto-invite-knocker-replication-crash`

Starting fresh from `main`.

---

## Problem Summary

**User scenario**: Course admin leaves the entire course (which recursively leaves all subchats/activities). After rejoining the course (via fix for #84), they try to rejoin subchats/activities through:
1. Course chat list "join" button → fails
2. Activity card "join open session" button → fails

**Why it fails**: Subchats/activities use `knock_restricted` join rule with the parent course as the allowed room. Standard Matrix `/join` SHOULD work when the user is back in the parent course, but there are cases where it doesn't — particularly when no remaining room member has invite power (all admins left) and the join falls back to a knock that nobody can approve.

**Room join rules**:
| Room Type | Join Rule | Allow List |
|---|---|---|
| Course space | `knock` | None |
| Course subchats | `knock_restricted` | Parent course room |
| Activity rooms | `knock_restricted` | Parent course room |

**Client join mechanisms** (all failing paths use standard Matrix `/join`, NOT `knock_with_code`):
| Scenario | File | Method | API |
|---|---|---|---|
| Subchat join from course chat list | `public_room_bottom_sheet.dart` | `_joinRoom()` | `client.joinRoom()` → Matrix `/join` |
| Activity "join open session" | `activity_session_start_page.dart` | `joinExistingSession()` | `client.joinRoom()` → Matrix `/join` |

## Why the Previous Fix (PR #9) Crashed Staging

The fix added knock event handling to `on_new_event`: when someone knocks, auto-invite them using `invite_user_to_room()` → `get_inviter_user()` → `promote_user_to_admin()`.

**`promote_user_to_admin()` used `_persist_events()`** — an internal Synapse API that writes directly to the database and publishes to the replication stream, bypassing the normal event pipeline. In a worker deployment:

1. Knock event → `on_new_event` fires
2. Background process → `promote_user_to_admin` → `_persist_events()` (PL change) ⚠️
3. PL event published to replication stream
4. `invite_user_to_room` → `update_room_membership(invite)` → waits for replication
5. Invite event → `on_new_event` fires again → `_retry_make_join` → `update_room_membership(join)`
6. **4 events in rapid succession**, some bypassing replication, some waiting on it → replication stream timeout → CPU cascade → staging freeze

**Key factors**:
- `_persist_events()` bypassed normal event pipeline (replication-unsafe)
- Event cascade: one knock produced 4 events via callbacks
- No cooldown or rate limiting — every knock processed immediately
- Always-on — no config toggle to disable
- `_retry_make_join` retried 5x with exponential backoff, compounding failures

## Constraints

1. **`_persist_events()` is the ONLY way to change power levels when no user has permission**. Even Synapse's own Admin API (`MakeRoomAdminRestServlet`) uses `_persist_events()` for this case. Matrix auth fundamentally prevents PL changes without sufficient power.

2. **`_persist_events()` is safe in HTTP request handlers** — the `knock_with_code` endpoint also calls `invite_user_to_room()` → `promote_user_to_admin()` and has never caused issues. The problem is running it in `on_new_event` background processes.

3. **`update_room_membership()` (public ModuleApi) is generally safe** — `_retry_make_join` uses it for invite auto-accept and works fine. The issue was the COMBINATION with `_persist_events()` and event cascading.

---

## Proposed Fix: New HTTP Endpoint + Client Fallback

### Architecture

Instead of reacting to knock events in `on_new_event` (dangerous), expose a new HTTP endpoint that the client calls when `/join` fails. This keeps `_persist_events()` in a safe HTTP request context.

```
Client: joinRoom() fails
  → Client calls POST /_synapse/client/unstable/org.pangea/v1/request_auto_join
    → Server: invite_user_to_room() (promote if needed, send invite)
    → auto_accept_invite handles the invite → user joins
```

### Server-Side Changes (synapse-pangea-chat-modules)

#### 1. New endpoint: `RequestAutoJoin`

**File**: `synapse_pangea_chat/request_auto_join/` (new sub-module)

```
POST /_synapse/client/unstable/org.pangea/v1/request_auto_join
Body: { "room_id": "!abc:example.com" }
Response: { "message": "Invited user", "room_id": "!abc:example.com" }
```

**Logic**:
1. Authenticate the requester (Matrix token)
2. Validate `room_id` exists
3. Validate the requester was previously a member (check membership history for `leave` or `knock` state)
4. Call `invite_user_to_room()` (which uses `get_inviter_user()` → `promote_user_to_admin()` if needed)
5. Return success — the existing `auto_accept_invite` callback will auto-join the user

**Safety features**:
- Rate limited (same as `knock_with_code`: 10 req/60s per user)
- Only processes users who were previously members (not random joins)
- Runs in HTTP request context (no background process, no event cascade)
- `_persist_events()` executes in the same way as it does in `knock_with_code` (which is proven safe)

#### 2. Remove the `on_new_event` knock handler from commit `3c9aa0c`

Revert the knock-handling code in `auto_accept_invite/__init__.py` back to invite-only handling. Keep `promote_user_to_admin` in `get_inviter_user.py` (it's needed by both `knock_with_code` and the new endpoint).

#### 3. Register the new endpoint in `PangeaChat.__init__`

Same pattern as `KnockWithCode` and `RequestRoomCode` registration.

#### 4. Add config options

```python
# in PangeaChatConfig
request_auto_join_requests_per_burst: int = 10
request_auto_join_burst_duration_seconds: int = 60
```

### Client-Side Changes (Flutter)

#### 1. Add `requestAutoJoin` API method

In the HTTP client (`lib/pangea/common/network/`), add a method that calls the new endpoint.

#### 2. Fallback in subchat/activity join flows

In `public_room_bottom_sheet.dart` (`_joinRoom`):
```dart
try {
  await client.joinRoom(roomId, via: via);
} catch (e) {
  // If /join fails, try auto-join endpoint
  await pangea.requestAutoJoin(roomId);
}
```

In `activity_session_start_page.dart` (`joinExistingSession`):
```dart
try {
  await courseParent!.client.joinRoom(sessionId, via: via);
} catch (e) {
  await pangea.requestAutoJoin(sessionId);
}
```

### E2E Test

**File**: `tests/test_request_auto_join_e2e.py`

Test scenario:
1. User1 (admin, PL 100) creates a `knock_restricted` room
2. User2 joins via invite
3. Power levels set (invite requires 50)
4. User1 leaves
5. User1 calls `POST /request_auto_join` with the room_id
6. Assert: User1 is auto-invited and auto-joined back into the room

---

## Implementation Order

1. **Server: New endpoint** — `request_auto_join` sub-module with rate limiting, auth, and membership history check
2. **Server: Remove `on_new_event` knock handler** — revert commit `3c9aa0c`'s knock-handling additions only (keep the auto-invite-on-invite logic)
3. **Server: E2E test** — verify the endpoint works in isolation
4. **Client: API method + fallback** — add `requestAutoJoin` and wire it into failing join paths
5. **Deploy to staging** — test with real course admin leave/rejoin scenarios

## Why This Is Safe

| Concern | How it's addressed |
|---|---|
| `_persist_events()` in background process | Moved to HTTP handler (same context as `knock_with_code`) |
| Event cascade from `on_new_event` | Removed — no more knock handling in callbacks |
| Rapid-fire from events | Client-initiated — one call per user action |
| No cooldown | Rate limited at endpoint level |
| Always-on | Endpoint is opt-in by client calling it |
| Retry storms | No server-side retries — client controls retry logic |

## Alternative Considered: Keep `on_new_event` with Safeguards

The decommissioned branch had: opt-in config, cooldowns, removed promote. This fails because **without promote, the auto-invite silently fails when no member has invite power** — which is the exact scenario we need to fix. Adding promote back into `on_new_event` recreates the crash risk regardless of safeguards.

## Alternative Considered: Client-Only Fix (`knock_with_code`)

Have the client fall back to `knock_with_code` when `/join` fails. Problems:
- `knock_with_code` requires a room code — subchats/activities don't have their own codes
- Would need to discover the parent course's code — adds complexity
- `knock_with_code` invites into the COURSE, not the subchat
- Doesn't solve the subchat-level rejoin