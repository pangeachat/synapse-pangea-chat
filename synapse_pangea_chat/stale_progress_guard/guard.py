"""The stale course progress guard: a check on every locally created event
that corrects a ``pangea.course_progress`` write naming a retired Can-Do id,
and the server-admin endpoint that loads the environment's map.

Design: stale-progress-guard.instructions.md.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.events import EventBase
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from synapse.util.frozenutils import unfreeze
from twisted.web.resource import Resource

from synapse_pangea_chat.migrate_course_can_dos.migrate_course_can_dos import (
    parse_can_do_map,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.sentry_capture import capture_exception
from synapse_pangea_chat.stale_progress_guard.correct import (
    COURSE_MISSIONS,
    COURSE_PROGRESS,
    TEACHER_MODE,
    correct_record,
    retired_ids_in_course_state,
)
from synapse_pangea_chat.stale_progress_guard.retired_map import RetiredCanDoMap

logger = logging.getLogger("synapse.module.synapse_pangea_chat.stale_progress_guard")

PATH = "/_synapse/client/pangea/v1/retired_can_dos"
_WATCHED = frozenset((COURSE_PROGRESS, COURSE_MISSIONS, TEACHER_MODE))
_ALLOW: Tuple[bool, Optional[dict]] = (True, None)


class RetiredCanDoWritten(Exception):
    """A course list or pins event naming retired Can-Dos was accepted. Its
    message holds the event type, the room and a count, never content."""


class StaleProgressGuard:
    def __init__(self, api: ModuleApi, retired: Any):
        self._api = api
        self._retired = retired

    def register(self) -> None:
        # Registered after every other check_event_allowed callback: Synapse
        # stops at the first callback that returns a replacement.
        self._api.register_third_party_rules_callbacks(
            check_event_allowed=self.check_event_allowed,
        )

    async def check_event_allowed(
        self,
        event: EventBase,
        state_events: Mapping[Tuple[str, str], EventBase],
    ) -> Tuple[bool, Optional[dict]]:
        """Every locally created event passes through here; all but the three
        watched types return at once."""
        if event.type not in _WATCHED:
            return _ALLOW
        try:
            return await self._check(event, state_events)
        # silent-ok: captured; a write that fails the check is stored as sent,
        # since raising here would refuse it with a 500, and the nightly pass
        # repairs a stale record
        except Exception as e:
            logger.exception(
                "stale_progress_guard: checking %s in %s failed",
                event.type,
                event.room_id,
            )
            capture_exception(e)
            return _ALLOW

    async def _check(
        self,
        event: EventBase,
        state_events: Mapping[Tuple[str, str], EventBase],
    ) -> Tuple[bool, Optional[dict]]:
        if not event.is_state() or not self._api.is_mine(event.sender):
            return _ALLOW
        retired = await self._retired.current()
        if not retired:
            return _ALLOW
        if event.type != COURSE_PROGRESS:
            count = retired_ids_in_course_state(event.type, event.content, retired)
            if count:
                logger.warning(
                    "stale_progress_guard: %s in %s names %d retired Can-Dos",
                    event.type,
                    event.room_id,
                    count,
                )
                capture_exception(
                    RetiredCanDoWritten(
                        f"{event.type} in {event.room_id} names {count} "
                        "retired Can-Dos"
                    )
                )
            return _ALLOW
        assert event.state_key is not None
        stored = state_events.get((COURSE_PROGRESS, event.state_key))
        correction = correct_record(
            event.content, stored.content if stored is not None else None, retired
        )
        if correction.content is None:
            return _ALLOW
        logger.info(
            "stale_progress_guard: corrected course progress for %s in %s from %s: "
            "retired_keys=%d kept_from_stored=%d completions_kept=%d",
            event.state_key,
            event.room_id,
            event.sender,
            correction.report["retired_keys"],
            correction.report["kept_from_stored"],
            correction.report["completions_kept"],
        )
        replacement = unfreeze(event.get_dict())
        replacement["content"] = correction.content
        return True, replacement


class RetiredCanDos(Resource):
    """``GET``, ``PUT`` and ``DELETE`` the environment's retired Can-Do map;
    server admins only."""

    isLeaf = True

    def __init__(self, api: ModuleApi, retired: RetiredCanDoMap):
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()
        self._retired = retired

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._handle, request, "GET")
        return server.NOT_DONE_YET

    def render_PUT(self, request: SynapseRequest):
        run_in_background(self._handle, request, "PUT")
        return server.NOT_DONE_YET

    def render_DELETE(self, request: SynapseRequest):
        run_in_background(self._handle, request, "DELETE")
        return server.NOT_DONE_YET

    async def _handle(self, request: SynapseRequest, method: str) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
        # silent-ok: the caller's auth failure, answered 401 (logged at INFO)
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
            return
        try:
            operator = requester.user.to_string()
            if not await self._api.is_user_admin(operator):
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return
            if method == "PUT":
                body = await extract_body_json(request)
                can_do_map, error = parse_can_do_map(
                    body.get("can_do_map") if isinstance(body, dict) else None
                )
                if can_do_map is None:
                    respond_with_json(request, 400, {"error": error}, send_cors=True)
                    return
                await self._retired.replace(can_do_map)
                logger.info(
                    "retired_can_dos: %s loaded %d ids", operator, len(can_do_map)
                )
            elif method == "DELETE":
                await self._retired.clear()
                logger.info("retired_can_dos: %s cleared the map", operator)
            self._retired.invalidate()
            current: Dict[str, str] = dict(await self._retired.current())
            respond_with_json(
                request,
                200,
                {"count": len(current), "can_do_map": current},
                send_cors=True,
            )
        except Exception as e:
            logger.exception("Error handling the retired Can-Do map")
            capture_exception(e)
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )


def register_stale_progress_guard(api: ModuleApi) -> StaleProgressGuard:
    hs = api._hs
    retired = RetiredCanDoMap(hs.get_datastores().main.db_pool, hs.get_clock())
    guard = StaleProgressGuard(api, retired)
    guard.register()
    api.register_web_resource(path=PATH, resource=RetiredCanDos(api, retired))
    return guard
