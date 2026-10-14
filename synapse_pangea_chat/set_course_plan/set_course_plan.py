"""Server-admin endpoint: point an existing course space at a different quest
and rename it to match.

``POST /_synapse/client/pangea/v1/set_course_plan``

Design: set-course-plan.instructions.md. The space's own copy of its Can-Do
list (``pangea.course_missions``) is replaced for the new quest first, so the
course never lists the old quest's Can-Dos. The events are sent as the space's
own strongest local instructor (``joined_local_instructors``, power level 100,
which readers require of the copy's sender), so nobody joins and nobody's power
level changes; the operator is recorded in the log line instead.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from synapse.api.constants import EventTypes
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

from synapse_pangea_chat.email_invite.course_missions import (
    COURSE_MISSIONS_STATE_EVENT_TYPE,
    CourseMissionsReader,
)
from synapse_pangea_chat.public_courses.backfill_l2 import (
    CANONICAL_PLAN_ID_KEY,
    LEGACY_PLAN_ID_KEYS,
)
from synapse_pangea_chat.public_courses.get_public_courses import (
    DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
    extract_plan_id,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.instructor_access import joined_local_instructors

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.set_course_plan.set_course_plan"
)

MAX_ID_LENGTH = 255
MAX_TITLE_LENGTH = 255
MAX_DESCRIPTION_LENGTH = 4096

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


def copy_revision(content: Optional[Mapping[str, Any]]) -> Optional[int]:
    """A course copy's revision, or None when there is no readable copy."""
    revision = content.get("revision") if content is not None else None
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
        return revision
    return None


def course_copy_content(
    built: Optional[Dict[str, Any]], current: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """The course copy to write for the new quest: the copy built from the CMS
    at one revision above the current copy's (1 when there is none), so an open
    Studio refuses a stale publish. Empty content when it could not be built,
    so readers fall back to the new shared quest rather than the old Can-Dos."""
    if built is None:
        return {}
    return {**built, "revision": (copy_revision(current) or 0) + 1}


def topic_content(text: str) -> Dict[str, Any]:
    """Topic content in the shape Synapse's own createRoom writes."""
    return {"topic": text, "m.topic": {"m.text": [{"body": text}]}}


def planned_writes(
    update: PlanUpdate,
    plan_event_type: str,
    copy: Dict[str, Any],
    current_name: Optional[str],
    current_topic: Optional[str],
    title: str,
    description: str,
) -> List[Tuple[str, Dict[str, Any]]]:
    """The state events to send, in order: when the plan changes, the course
    copy and then the course plan; then the name, then the topic, each only
    when it differs from what the space has.

    The copy goes first so a refusal part-way leaves the plan on the old quest,
    and sending the same request again finishes the job. Once the plan points
    at the new quest the copy is never rewritten, so a re-run cannot wipe the
    teacher's later edits."""
    writes: List[Tuple[str, Dict[str, Any]]] = []
    if update.outcome == UPDATE and update.content is not None:
        writes.append((COURSE_MISSIONS_STATE_EVENT_TYPE, copy))
        writes.append((plan_event_type, update.content))
    if current_name != title:
        writes.append((EventTypes.Name, {"name": title}))
    if (current_topic or "") != description:
        writes.append((EventTypes.Topic, topic_content(description)))
    return writes


def _state_text(event: Any, key: str) -> Optional[str]:
    """A string field of a state event's content, or None."""
    if event is None or not isinstance(event.content, Mapping):
        return None
    value = event.content.get(key)
    return value if isinstance(value, str) else None


def _valid_id(value: Any) -> Optional[str]:
    """The value when it is a usable id, else None."""
    if isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH:
        return value
    return None


class SetCoursePlanRequest(NamedTuple):
    room_id: str
    quest_id: str
    expected_quest_id: str
    title: str
    description: str
    dry_run: bool


def parse_request(
    body: Any,
) -> Tuple[Optional[SetCoursePlanRequest], Optional[str]]:
    """``(request, None)`` or ``(None, error)``."""
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
    title = body.get("title")
    if not isinstance(title, str) or not 0 < len(title) <= MAX_TITLE_LENGTH:
        return None, "'title' must be a non-empty string"
    description = body.get("description")
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
        return None, "'description' must be a string"
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return None, "'dry_run' must be a boolean"
    return (
        SetCoursePlanRequest(
            room_id, quest_id, expected_quest_id, title, description, dry_run
        ),
        None,
    )


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
        self._course_missions = CourseMissionsReader(
            api, config.cms_base_url, config.cms_service_api_key
        )

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest) -> None:
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

            req, error = parse_request(await extract_body_json(request))
            if req is None:
                respond_with_json(request, 400, {"error": error}, send_cors=True)
                return
            room_id = req.room_id

            # The whole state: choosing the sender needs every member and the
            # power levels, beside the four events this endpoint reads.
            state = await self._api.get_room_state(room_id)
            event = state.get((self._event_type, ""))
            update = plan_update(
                dict(event.content) if event is not None else None,
                req.expected_quest_id,
                req.quest_id,
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

            current_name = _state_text(state.get((EventTypes.Name, "")), "name")
            current_topic = _state_text(state.get((EventTypes.Topic, "")), "topic")
            copy: Dict[str, Any] = {}
            copy_report: Optional[Dict[str, Any]] = None
            if update.outcome == UPDATE:
                current_copy = state.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
                current_copy_content = (
                    dict(current_copy.content)
                    if current_copy is not None
                    and isinstance(current_copy.content, Mapping)
                    else None
                )
                built = await self._course_missions.read(req.quest_id)
                if built is None:
                    logger.warning(
                        "set_course_plan: no course copy built for quest %s in %s; "
                        "an empty copy is written so readers use the shared quest",
                        req.quest_id,
                        room_id,
                    )
                copy = course_copy_content(built, current_copy_content)
                copy_report = {
                    "built": built is not None,
                    "previous_revision": copy_revision(current_copy_content),
                    "revision": copy_revision(copy),
                    "missions": len(copy.get("missions") or []),
                }
            writes = planned_writes(
                update,
                self._event_type,
                copy,
                current_name,
                current_topic,
                req.title,
                req.description,
            )
            answer = {
                "room_id": room_id,
                "previous_quest_id": update.current_quest_id,
                "quest_id": req.quest_id,
                "previous_name": current_name,
                "previous_topic": current_topic,
                "changes": [event_type for event_type, _ in writes],
                "course_copy": copy_report,
                "dry_run": req.dry_run,
            }
            if not writes:
                respond_with_json(
                    request,
                    200,
                    {**answer, "sender": None, "changed": False},
                    send_cors=True,
                )
                return

            # One instructor sends every write: readers ignore a course copy
            # sent below power level 100, so a weaker sender is never used.
            instructors = joined_local_instructors(self._api, state)
            if not instructors:
                logger.warning(
                    "set_course_plan: no local instructor in %s can send %s; "
                    "refused, nothing written",
                    room_id,
                    answer["changes"],
                )
                respond_with_json(
                    request,
                    409,
                    {"error": "No local instructor can send the course events"},
                    send_cors=True,
                )
                return
            sender = instructors[0]

            written: List[str] = []
            if not req.dry_run:
                for event_type, content in writes:
                    try:
                        await self._api.create_and_send_event_into_room(
                            {
                                "type": event_type,
                                "state_key": "",
                                "room_id": room_id,
                                "sender": sender,
                                "content": content,
                            }
                        )
                    except AuthError as e:
                        # The room checks the sender again at send time: one
                        # who left or lost power since they were chosen is
                        # refused before this event is written.
                        logger.error(
                            "set_course_plan: %s refused %s sent as %s; "
                            "already written: %s: %s",
                            room_id,
                            event_type,
                            sender,
                            written,
                            e,
                        )
                        respond_with_json(
                            request,
                            409,
                            {
                                "error": "The room refused a write",
                                "errcode": e.errcode,
                                "refused": event_type,
                                "written": written,
                            },
                            send_cors=True,
                        )
                        return
                    except Exception:
                        logger.exception(
                            "set_course_plan: writing %s to %s failed; "
                            "already written: %s",
                            event_type,
                            room_id,
                            written,
                        )
                        respond_with_json(
                            request,
                            500,
                            {"error": "Internal server error", "written": written},
                            send_cors=True,
                        )
                        return
                    written.append(event_type)
            logger.info(
                "set_course_plan: operator=%s room=%s %s -> %s changes=%s "
                "sent_as=%s dry_run=%s",
                operator,
                room_id,
                update.current_quest_id,
                req.quest_id,
                answer["changes"],
                sender,
                req.dry_run,
            )
            respond_with_json(
                request,
                200,
                {**answer, "sender": sender, "changed": not req.dry_run},
                send_cors=True,
            )
        except Exception:
            logger.exception("Error setting a course plan")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )
