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
from synapse.events import EventBase
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from synapse.util.duration import Duration
from twisted.web.resource import Resource

from synapse_pangea_chat.course_trip.cities import CityListError, CityListReader
from synapse_pangea_chat.course_trip.slicing import TRIP_VERSION, TripSlicer, report
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
from synapse_pangea_chat.room_code.instructor_access import (
    joined_local_instructors,
    user_power,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.course_trip.slice_course"
)

try:
    import sentry_sdk  # type: ignore[import-not-found]
except ImportError:  # silent-ok: Sentry is optional in the module's environments
    sentry_sdk = None


def _capture_exception(e: Exception) -> None:
    # Background work never reaches Synapse's request-level Sentry capture.
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


#: The power level a copy's sender must hold for the copy to count.
INSTRUCTOR_POWER_LEVEL = 100


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
            force = body.get("force", False) if isinstance(body, dict) else False
            if not isinstance(room_id, str) or not room_id.startswith("!"):
                respond_with_json(
                    request,
                    400,
                    {"error": "'room_id' must be a room ID"},
                    send_cors=True,
                )
                return
            if not isinstance(dry_run, bool) or not isinstance(force, bool):
                respond_with_json(
                    request,
                    400,
                    {"error": "'dry_run' and 'force' must be booleans"},
                    send_cors=True,
                )
                return
            code, answer = await self.apply(room_id, operator, dry_run, force)
            respond_with_json(request, code, answer, send_cors=True)
        except Exception:
            logger.exception("Error slicing a course")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def apply(
        self,
        room_id: str,
        operator: str,
        dry_run: bool,
        force: bool = False,
        require_copy: bool = False,
    ) -> Tuple[int, Dict[str, Any]]:
        state = await self._api.get_room_state(room_id)
        plan = _content(state.get((self._plan_type, "")))
        quest_id = extract_plan_id(plan) if plan is not None else None
        if quest_id is None:
            return 404, {"error": "Room has no course plan"}
        copy_event = state.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
        # A copy counts only when a course admin sent it, as every reader
        # counts it; anything else is no copy.
        existing = (
            _content(copy_event)
            if copy_event is not None
            and user_power(state, copy_event.sender) >= INSTRUCTOR_POWER_LEVEL
            else None
        )
        if existing == {}:
            existing = None
        if require_copy and existing is None:
            return 409, {"room_id": room_id, "error": "The course has no copy to slice"}
        if (
            existing is not None
            and existing.get("version") == TRIP_VERSION
            and not force
        ):
            # Sliced already, or published as a trip from the dashboard: the
            # one-time pass never re-slices a teacher's cities.
            return 409, {
                "room_id": room_id,
                "error": "The course copy is already a trip",
                "already_sliced": True,
            }
        try:
            index = await self._cities.index()
        except CityListError as e:
            logger.error("slice_course: the city list could not be read: %s", e)
            return 503, {"error": "The city list could not be read"}
        listed = [
            activity_id
            for entry in (existing or {}).get("missions") or []
            if isinstance(entry, Mapping)
            for activity_id in entry.get("activity_ids") or []
            if isinstance(activity_id, str)
        ]
        try:
            built, points = await self._reader.read_for_slice(quest_id, listed)
        except CourseMissionsError as e:
            logger.error(
                "slice_course: no copy could be built for %s in %s: %s",
                quest_id,
                room_id,
                e,
            )
            return 503, {"error": "The course copy could not be built from the CMS"}
        copy, slices = TripSlicer(index).slice_copy(built, points, existing)
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
            now_event = again.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
            if (now_event.event_id if now_event is not None else None) != (
                copy_event.event_id if copy_event is not None else None
            ):
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


class AutoSlice:
    """Slices a course's own copy as soon as an app writes it as version 1,
    so every course that gets a copy outside the dashboard is sliced by this
    one server path (course-trip.instructions.md), not by each app. Only on
    with ``course_missions_v2_enabled``. A copy that is already a trip, or
    that no course admin sent, is left alone."""

    OPERATOR = "auto:new-copy"

    def __init__(self, api: ModuleApi, slice_course: SliceCourse) -> None:
        self._api = api
        self._clock = api._hs.get_clock()
        self._slice = slice_course
        api.register_third_party_rules_callbacks(on_new_event=self.on_new_event)

    async def on_new_event(
        self, event: EventBase, _state: Mapping[Any, EventBase]
    ) -> None:
        """Awaited inline for every event on the homeserver, so this decides
        from the event alone and hands the slice to a background task."""
        try:
            content = event.content
            if (
                event.type == COURSE_MISSIONS_STATE_EVENT_TYPE
                and event.is_state()
                and event.state_key == ""
                and isinstance(content, Mapping)
                and content.get("version") == 1
                and content.get("missions")
            ):
                self._clock.call_later(
                    Duration(milliseconds=0),
                    self._api.run_as_background_process,
                    "course_trip_auto_slice",
                    self.slice,
                    event.room_id,
                )
        except Exception as e:
            # A failure here must never reject or delay the event itself.
            logger.exception(
                "course_trip auto-slice hook failed for %s in %s",
                event.event_id,
                event.room_id,
            )
            _capture_exception(e)

    async def slice(self, room_id: str) -> None:
        code, answer = await self._slice.apply(
            room_id, self.OPERATOR, dry_run=False, require_copy=True
        )
        if code == 200:
            return
        # Refused (no city list, no local instructor, a newer copy): the course
        # keeps its unsliced copy and the one-time pass can slice it later.
        logger.warning(
            "course_trip auto-slice of %s refused (%s): %s",
            room_id,
            code,
            answer.get("error"),
        )
