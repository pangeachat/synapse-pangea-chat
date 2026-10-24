"""The two endpoints admin-dash-api calls, both behind the shared secret.

* ``GET  /_synapse/client/pangea/v1/analytics_forward/head``: the persisted
  stream position and this module's clock, read before a Refresh is
  acknowledged. The Refresh waits until the ingest has been delivered through
  both (``delivered_through`` and ``profiles_clean_before``).
* ``POST /_synapse/client/pangea/v1/analytics_state``: rooms' current state of
  the forwarded types and users' profiles, for bootstrap and the nightly
  reconcile. Every record carries the same ordering keys as a pushed one, so a
  snapshot and a concurrent push merge through one newer-wins rule.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Callable, Dict, List, Optional

from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from twisted.web.resource import Resource

from synapse_pangea_chat.analytics_forward.wire import (
    STATE_TYPES,
    event_record,
    profile_record,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

logger = logging.getLogger("synapse.modules.synapse_pangea_chat.analytics_forward")

HEAD_PATH = "/_synapse/client/pangea/v1/analytics_forward/head"
STATE_PATH = "/_synapse/client/pangea/v1/analytics_state"
#: Per request. A course's rooms and roster fit many times over.
MAX_IDS = 500


class _SecretResource(Resource):
    isLeaf = True

    def __init__(self, secret: str) -> None:
        super().__init__()
        self._secret = secret.encode("utf-8")

    def _authorized(self, request: SynapseRequest) -> bool:
        header = request.getHeader("Authorization") or ""
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(
            header[len("Bearer ") :].encode("utf-8"), self._secret
        )

    def _run(self, request: SynapseRequest, handler: Any) -> int:
        async def _guarded() -> None:
            try:
                if not self._authorized(request):
                    respond_with_json(request, 401, {"error": "Unauthorized"})
                    return
                status, body = await handler(request)
                respond_with_json(request, status, body)
            except Exception:  # silent-ok: logged with its traceback, answered 500
                logger.exception("analytics_forward endpoint failed")
                respond_with_json(request, 500, {"error": "Internal error"})

        run_in_background(_guarded)
        return server.NOT_DONE_YET


class ForwardHead(_SecretResource):
    def __init__(
        self,
        secret: str,
        store: Any,
        clock_ms: Any,
        wake: Optional[Callable[[bool], None]] = None,
    ) -> None:
        super().__init__(secret)
        self._store = store
        self._clock_ms = clock_ms
        self._wake = wake

    def render_GET(self, request: SynapseRequest) -> int:
        return self._run(request, self._head)

    async def _head(self, request: SynapseRequest) -> Any:
        from synapse_pangea_chat.analytics_forward.forwarder import refresh_barrier

        head = self._store.head()
        forwarded_head = await self._store.forwarded_head(head)
        any_dirty = await self._store.any_dirty()
        delivered_clean = await self._store.delivered_clean_before()
        # The clock after the outbox read: a batch passes a dirty barrier only
        # if its own clock (taken before its outbox read) is no earlier.
        now = int(self._clock_ms())
        barrier_pos, barrier_time = refresh_barrier(
            forwarded_head, any_dirty, delivered_clean, now
        )
        if self._wake is not None:
            # Where the forwarder runs in this process, anything undelivered
            # goes now rather than at the next poll, and a profile barrier
            # gets a batch read after it. wake() coalesces a burst of head
            # reads into one run; with nothing undelivered, nothing is sent.
            self._wake(any_dirty)
        return 200, {
            "stream_pos": head,
            "module_time_ms": now,
            "barrier_pos": barrier_pos,
            "barrier_time": barrier_time,
        }


def _id_list(body: Dict[str, Any], key: str, sigil: str) -> Optional[List[str]]:
    value = body.get(key, [])
    if not isinstance(value, list) or len(value) > MAX_IDS:
        return None
    if not all(isinstance(v, str) and v.startswith(sigil) and ":" in v for v in value):
        return None
    return list(dict.fromkeys(value))


class AnalyticsState(_SecretResource):
    def __init__(self, secret: str, store: Any, state_controller: Any) -> None:
        super().__init__(secret)
        from synapse.types.state import StateFilter

        self._store = store
        self._state = state_controller
        self._filter = StateFilter.from_types([(t, None) for t in sorted(STATE_TYPES)])

    def render_POST(self, request: SynapseRequest) -> int:
        return self._run(request, self._snapshot)

    async def _snapshot(self, request: SynapseRequest) -> Any:
        body = await extract_body_json(request)
        if not isinstance(body, dict):
            return 400, {"error": "Body must be a JSON object"}
        room_ids = _id_list(body, "room_ids", "!")
        user_ids = _id_list(body, "user_ids", "@")
        if room_ids is None or user_ids is None:
            return 400, {
                "error": f"room_ids and user_ids must be lists of at most {MAX_IDS} "
                "Matrix room / user ids"
            }
        rooms: Dict[str, Any] = {}
        for room_id in room_ids:
            state = await self._state.get_current_state(
                room_id, state_filter=self._filter
            )
            if ("m.room.create", "") not in state:
                rooms[room_id] = None
                continue
            events = sorted(
                (
                    event_record(event)
                    for event in state.values()
                    if isinstance(event.internal_metadata.stream_ordering, int)
                ),
                key=lambda record: record["stream_pos"],
            )
            rooms[room_id] = {"events": events}
        # The revision first, then the read: a change committed after the
        # bump bumps again, so whatever this read returns is never newer than
        # a later push's revision says.
        revisions = await self._store.bump_revisions(user_ids)
        profiles = await self._store.profiles(user_ids)
        return 200, {
            "rooms": rooms,
            "profiles": [
                profile_record(user_id, revisions[user_id], profiles.get(user_id))
                for user_id in user_ids
            ],
        }
