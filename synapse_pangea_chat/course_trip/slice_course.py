"""``POST /_synapse/client/pangea/v1/slice_course`` (server admins): slice one
existing course into cities, the migration of pangeachat/.github#780.

For the space's quest, each Can-Do's city slice is pinned by the Create my
class rule (the city where the Can-Do has the most activities in the course's
language and level) and ``pangea.course_missions`` version 2 is written,
undated, keeping the existing copy's notes, due dates and removed lists. It
sends as the space's strongest local instructor at power level 100, as
set-course-plan does; a space with no local instructor is refused and
reported. The response reports each Can-Do as viable (three pinned or more),
thin or empty: the generation backlog. A dry run does everything but the
write. One space per call; the operator script loops.
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
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.course_trip.cities import CityListError, CityListReader
from synapse_pangea_chat.course_trip.slicing import TripSlicer, report
from synapse_pangea_chat.email_invite.course_missions import (
    COURSE_MISSIONS_STATE_EVENT_TYPE,
    CourseMissionsError,
    CourseMissionsReader,
)
from synapse_pangea_chat.public_courses.get_public_courses import (
    DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
    extract_plan_id,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.instructor_access import joined_local_instructors

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.course_trip.slice_course"
)


def _content(event: Any) -> Optional[Dict[str, Any]]:
    if event is None or not isinstance(event.content, Mapping):
        return None
    return dict(event.content)


class SliceCourse(Resource):
    isLeaf = True

    def __init__(
        self,
        api: ModuleApi,
        config: Any,
        cities: CityListReader,
    ) -> None:
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()
        self._plan_type = (
            getattr(config, "course_plan_state_event_type", None)
            or DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE
        )
        self._cities = cities
        self._reader = CourseMissionsReader(
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
            body = await extract_body_json(request)
            room_id = body.get("room_id") if isinstance(body, dict) else None
            dry_run = body.get("dry_run", False) if isinstance(body, dict) else False
            if not isinstance(room_id, str) or not room_id.startswith("!"):
                respond_with_json(
                    request,
                    400,
                    {"error": "'room_id' must be a room ID"},
                    send_cors=True,
                )
                return
            if not isinstance(dry_run, bool):
                respond_with_json(
                    request,
                    400,
                    {"error": "'dry_run' must be a boolean"},
                    send_cors=True,
                )
                return
            code, answer = await self.apply(room_id, operator, dry_run)
            respond_with_json(request, code, answer, send_cors=True)
        except Exception:
            logger.exception("Error slicing a course")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def apply(
        self, room_id: str, operator: str, dry_run: bool
    ) -> Tuple[int, Dict[str, Any]]:
        state = await self._api.get_room_state(room_id)
        plan = _content(state.get((self._plan_type, "")))
        quest_id = extract_plan_id(plan) if plan is not None else None
        if quest_id is None:
            return 404, {"error": "Room has no course plan"}
        existing = _content(state.get((COURSE_MISSIONS_STATE_EVENT_TYPE, "")))
        if existing == {}:
            existing = None
        try:
            index = await self._cities.index()
        except CityListError as e:
            logger.error("slice_course: the city list could not be read: %s", e)
            return 503, {"error": "The city list could not be read"}
        try:
            built, docs = await self._reader.read_with_docs(quest_id)
        except CourseMissionsError as e:
            logger.error(
                "slice_course: no copy could be built for %s in %s: %s",
                quest_id,
                room_id,
                e,
            )
            return 503, {"error": "The course copy could not be built from the CMS"}
        copy, slices = TripSlicer(index).slice_copy(built, docs, existing)
        instructors = joined_local_instructors(self._api, state)
        answer: Dict[str, Any] = {
            "room_id": room_id,
            "quest_id": quest_id,
            "previous_revision": existing.get("revision") if existing else None,
            "revision": copy["revision"],
            "report": report(slices),
            "dry_run": dry_run,
        }
        if not instructors:
            logger.warning(
                "slice_course: no local instructor in %s can send the copy; refused",
                room_id,
            )
            return 409, {
                **answer,
                "error": "No local instructor can send the course copy",
            }
        sender = instructors[0]
        if not dry_run:
            # The read can take seconds; a copy published in the meantime wins.
            again = await self._api.get_room_state(
                room_id, [(COURSE_MISSIONS_STATE_EVENT_TYPE, "")]
            )
            now = _content(again.get((COURSE_MISSIONS_STATE_EVENT_TYPE, "")))
            if (now or None) != existing:
                logger.warning(
                    "slice_course: %s changed while its copy was built; refused",
                    room_id,
                )
                return 409, {
                    **answer,
                    "error": "The space changed while its copy was built",
                }
            await self._api.create_and_send_event_into_room(
                {
                    "type": COURSE_MISSIONS_STATE_EVENT_TYPE,
                    "state_key": "",
                    "room_id": room_id,
                    "sender": sender,
                    "content": copy,
                }
            )
        logger.info(
            "slice_course: operator=%s room=%s quest=%s revision=%s viable=%s thin=%s empty=%s sent_as=%s dry_run=%s",
            operator,
            room_id,
            quest_id,
            copy["revision"],
            answer["report"]["viable"],
            answer["report"]["thin"],
            answer["report"]["empty"],
            sender,
            dry_run,
        )
        return 200, {**answer, "sender": sender, "changed": not dry_run}
