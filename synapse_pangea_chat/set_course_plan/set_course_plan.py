"""Server-admin endpoint: point an existing course space at a different quest.

``POST /_synapse/client/pangea/v1/set_course_plan``

Design: set-course-plan.instructions.md. The event is sent as the space's own
highest-powered local member (``select_state_sender``), so nobody joins and
nobody's power level changes; the operator is recorded in the log line instead.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Mapping, NamedTuple, Optional, Tuple

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.public_courses.backfill_l2 import (
    CANONICAL_PLAN_ID_KEY,
    LEGACY_PLAN_ID_KEYS,
)
from synapse_pangea_chat.public_courses.get_public_courses import (
    DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
    extract_plan_id,
)
from synapse_pangea_chat.public_courses.select_state_sender import select_state_sender
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.set_course_plan.set_course_plan"
)

MAX_ID_LENGTH = 255

NOT_A_COURSE = "not_a_course"
MISMATCH = "mismatch"
UNCHANGED = "unchanged"
UPDATE = "update"


class PlanUpdate(NamedTuple):
    outcome: str
    current_quest_id: Optional[str] = None
    # The content to write; set only when outcome is UPDATE.
    content: Optional[Dict[str, Any]] = None


def plan_update(
    content: Optional[Mapping[str, Any]],
    expected_quest_id: str,
    quest_id: str,
) -> PlanUpdate:
    """Decide what to do with a space's current course-plan content.

    The plan id is read with the catalog's own rule (``extract_plan_id``). A
    mismatch is checked before "already there", so a space the teacher moved
    since the operator looked is never touched.
    """
    if content is None:
        return PlanUpdate(NOT_A_COURSE)
    current = extract_plan_id(content)
    if current is None:
        return PlanUpdate(NOT_A_COURSE)
    if current != expected_quest_id:
        return PlanUpdate(MISMATCH, current)
    if current == quest_id:
        return PlanUpdate(UNCHANGED, current)
    new_content = {
        key: value for key, value in content.items() if key not in LEGACY_PLAN_ID_KEYS
    }
    new_content[CANONICAL_PLAN_ID_KEY] = quest_id
    return PlanUpdate(UPDATE, current, new_content)


def _valid_id(value: Any) -> Optional[str]:
    """The value when it is a usable id, else None."""
    if isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH:
        return value
    return None


def parse_request(
    body: Any,
) -> Tuple[Optional[Tuple[str, str, str, bool]], Optional[str]]:
    """``((room_id, quest_id, expected_quest_id, dry_run), None)`` or ``(None, error)``."""
    if not isinstance(body, dict):
        return None, "Request body must be a JSON object"
    room_id = _valid_id(body.get("room_id"))
    if room_id is None or not room_id.startswith("!"):
        return None, "'room_id' must be a room ID"
    quest_id = _valid_id(body.get("quest_id"))
    if quest_id is None:
        return None, "'quest_id' must be a non-empty string"
    expected_quest_id = _valid_id(body.get("expected_quest_id"))
    if expected_quest_id is None:
        return None, "'expected_quest_id' must be a non-empty string"
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return None, "'dry_run' must be a boolean"
    return (room_id, quest_id, expected_quest_id, dry_run), None


class SetCoursePlan(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()
        self._event_type = (
            config.course_plan_state_event_type
            or DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE
        )

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
            operator = requester.user.to_string()

            if not await self._api.is_user_admin(operator):
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return

            parsed, error = parse_request(await extract_body_json(request))
            if parsed is None:
                respond_with_json(request, 400, {"error": error}, send_cors=True)
                return
            room_id, quest_id, expected_quest_id, dry_run = parsed

            state = await self._api.get_room_state(room_id, [(self._event_type, "")])
            event = state.get((self._event_type, ""))
            update = plan_update(
                dict(event.content) if event is not None else None,
                expected_quest_id,
                quest_id,
            )

            if update.outcome == NOT_A_COURSE:
                respond_with_json(
                    request,
                    404,
                    {"error": "Room has no course plan"},
                    send_cors=True,
                )
                return
            if update.outcome == MISMATCH:
                respond_with_json(
                    request,
                    409,
                    {
                        "error": "Room points at a different quest than expected",
                        "current_quest_id": update.current_quest_id,
                    },
                    send_cors=True,
                )
                return
            if update.outcome == UNCHANGED:
                respond_with_json(
                    request,
                    200,
                    {
                        "room_id": room_id,
                        "previous_quest_id": update.current_quest_id,
                        "quest_id": quest_id,
                        "sender": None,
                        "changed": False,
                        "dry_run": dry_run,
                    },
                    send_cors=True,
                )
                return

            sender = await select_state_sender(self._api, room_id, self._event_type)
            if sender is None:
                logger.warning(
                    "set_course_plan: no local member can send %s in %s; "
                    "refused, nothing written",
                    self._event_type,
                    room_id,
                )
                respond_with_json(
                    request,
                    409,
                    {"error": "No local member can send the course plan event"},
                    send_cors=True,
                )
                return

            if not dry_run:
                await self._api.create_and_send_event_into_room(
                    {
                        "type": self._event_type,
                        "state_key": "",
                        "room_id": room_id,
                        "sender": sender,
                        "content": update.content,
                    }
                )
            logger.info(
                "set_course_plan: operator=%s room=%s %s -> %s sent_as=%s dry_run=%s",
                operator,
                room_id,
                update.current_quest_id,
                quest_id,
                sender,
                dry_run,
            )
            respond_with_json(
                request,
                200,
                {
                    "room_id": room_id,
                    "previous_quest_id": update.current_quest_id,
                    "quest_id": quest_id,
                    "sender": sender,
                    "changed": not dry_run,
                    "dry_run": dry_run,
                },
                send_cors=True,
            )
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
        except Exception:
            logger.exception("Error setting a course plan")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )
