# Synapse 1.124.0 → 1.159.0 upgrade notes

Audit trail for the version bump ([#175](https://github.com/pangeachat/synapse-pangea-chat/issues/175)).
What broke, how each fix was verified, and what was checked and found clean — so
nobody re-audits it. Companion inventory work: [pangeachat/ansible#235](https://github.com/pangeachat/ansible/issues/235).

## What broke (all fixed, all test-proven)

The full 357-test suite was run against both pins; on 1.159.0 it surfaced 14
failures across 6 areas before the fixes, and is green on both after.

| Surface | Break | Fix |
| --- | --- | --- |
| `export_user_data` | Synapse ≥1.150 passes `FilteredEvent` wrappers (event + membership annotation) to `write_events` instead of `EventBase`; `get_pdu_json()` crashed every export | Capability unwrap `getattr(e, "event", e)` in `JsonExfiltrationWriter.write_events`; unit tests cover both shapes |
| Event content reads | 1.159 events are Rust-backed: `event.content` is `builtins.JsonObject` — a `Mapping`, **not** a `dict` subclass — so direct `isinstance(content, dict)` checks read every event as empty | The only two such sites fixed to `Mapping`: `preview_with_code/get_preview.py` (`_content_dict`) and `public_courses/select_state_sender.py` |
| `delayed_push` | The monkey-patched `HttpPusher._unsafe_process` copy mirrored 1.124.0; on 1.159 the push counters require a `server_name` label (bare `.inc()` raises), backoff moved to `Clock.call_later(Duration)`, and the tracing span narrowed to `_process_one` | Body re-derived against 1.159.0 per the audit required by [delayed-push.instructions.md](../.github/instructions/delayed-push.instructions.md); audited version, config defaults, and tests moved to `1.159.0` |
| `public_courses` catalog / `create_course_space` (tests) | Synapse 1.126 flipped `room_list_publication_rules` to deny-all; the test homeserver rode the old default, so nothing could publish and the catalog was empty — the same break course discovery would hit in deployment | `tests/base_e2e.py` pins the allow-all rule (mirrors the inventories, per ansible#235) and doubles as the course-discovery canary |
| `activity_session_previews` (tests) | 1.159 added `rc_room_creation` (default **1 room/min, burst 10**); the suite's room churn hit 429s | Test config raises it. Deployment needs its own override — see constraints below |
| `public_courses/backfill_l2` | `Clock.call_later`/`Clock.sleep` received plain floats; 1.159's `Duration`-typed Clock calls `.as_secs()` on them at runtime. Caught by mypy, not tests (background repair path) | `_SecondsInterval` float subclass satisfying both contracts, same pattern as the existing `_LoopingCallInterval` |
| All module HTTP resources (found on staging post-deploy, not by the suite) | `defer.ensureDeferred(self._async_render_X(request))` leaks the request logcontext to the reactor; 1.159's hardened `clock.py` asserts the sentinel at every `looping_call` fire and **permanently kills** any Synapse timer firing in a leaked window (~1 death/min under module traffic on staging) | All 21 render methods switched to `synapse.logging.context.run_in_background` (identical on 1.124/1.159); regression test `tests/test_logcontext_e2e.py` asserts no leak markers in server logs |

## Key facts established (verify with these, don't re-derive)

- **Nested event-content values materialize as plain `dict`** on 1.159 — only
  *direct* `isinstance(event.content, dict)` checks break. Every
  `event.content.get(...)` call site and every nested `isinstance(x, dict)`
  check (power-level `users` maps etc.) was swept and is unaffected. Probe:

  ```python
  from synapse.events import make_event_from_dict
  from synapse.api.room_versions import RoomVersions
  ev = make_event_from_dict({...valid event dict...}, RoomVersions.V11)
  type(ev.content)                 # builtins.JsonObject on 1.159
  isinstance(ev.content, dict)     # False
  type(ev.content.get("nested"))   # dict
  ```

- **`ModuleApi.get_room_state` contract is unchanged** (same tuple keys, same
  `EventBase` values).
- **Default room version is still "11"** on 1.159 (`synapse/config/server.py`),
  so the inventories' `matrix_synapse_default_room_version: "11"` pin is
  belt-and-braces, not load-bearing yet.
- **`run_as_background_process`**: the direct import still works on 1.159; the
  existing signature-sniffing shim (`_RUN_AS_BG_SUPPORTS_SERVER_NAME`) handles
  the added `server_name` parameter. `ModuleApi.run_as_background_process` only
  exists from 1.136, so migrating to it is **post-fleet-upgrade cleanup**, not
  a bump prerequisite.
- **delayed_push audit results** (the four items the design doc requires):
  `_unsafe_process` — counter labels + span structure changed (re-derived);
  `_start_processing` — upstream added a failing-pusher guard, our wrapper
  delegates and is unaffected; pusher-cursor advancement — unchanged;
  presence semantics — unchanged (`presence_enabled` still on
  `hs.config.server`; `state == "online"` values are spec constants).

## Already checked in the original audit — don't re-audit

From #175: the Flutter client's media handling (avatars, images, voice
messages) and every database query the module makes were audited against the
1.124 → 1.159 window and are clean.

Upstream upgrade notes checked and **not applicable** to us: MSC3861/MAS
removal (we use built-in OIDC), `synapse-s3-storage-provider` ≥1.6.0 (not
enabled), quarantine-media stream writer (we don't route those endpoints to
workers), MSC2697 dehydrated devices, appservice `/register` under MAS,
Ubuntu/GPG-keyring notices (Docker deployment), Python 3.10+/SQLite floors
(official images).

## Verification commands

```bash
# Full suite against the 1.159 venv (and identically against a 1.124 venv)
PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:$PATH" \
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
.venv/bin/python -m unittest discover -s tests -t . -p 'test_*.py'
```

On a 1.124 install the delayed_push body tests skip (8 skips) by design — the
body mirrors 1.159.0 and the module refuses to enable the feature on any other
version.

## Deployment constraints

- **`delayed_push` moves in lockstep on staging.** This commit's patched body
  runs only on 1.159.0, and the guard pins that as a code constant
  (`AUDITED_SYNAPSE_VERSION` in `delayed_push/delayed_push.py`): the config's
  `require_synapse_version` can only confirm it, never select another version.
  Staging currently enables `delayed_push` with `require_synapse_version:
  "1.124.0"` in its inventory — deploying this module commit to staging
  *without* bumping Synapse and that config value in the same Ansible run makes
  Synapse **refuse to boot**, with an error naming the mismatch (a loud deploy
  failure, by design, instead of a healthy-looking server with broken push
  processing). Production has no `delayed_push` block and is unaffected.
- **`rc_room_creation` needs an inventory override** in both environments —
  the new upstream default (1 room/min, burst 10) is below real course-creation
  and bot room-creation bursts.
- **No rollback path once 1.159 runs**: the bump spans schema 89 → 94, far
  outside Synapse's downgrade window. Snapshot RDS before each environment's
  deploy; rollback = restore snapshot.
- **`COMPAT.yml` stays at `min_synapse_version: 1.124.0`** until production is
  on 1.159.0, then raise it to `1.159.0`.
