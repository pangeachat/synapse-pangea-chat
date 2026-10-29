"""Where an undated course's ship is: the busiest stop (org quests doc,
"Progression"; pangeachat/.github#780).

Each member's position lives in ``pangea.course_progress`` in their own
analytics room, readable only by them and teachers with analytics access, so
no client can count the others. A scheduled pass reads them here and writes
only the busiest stop into the course space, ``pangea.course_position``
``{stop, from_stop, since}``: indexes into the copy's ``missions[]`` (a route
may visit one city twice, and the busiest stop can move backward), never
counts and never anyone's position. Active members only (a progress record
written within the active window), each counted at their first unlocked
Can-Do, the earlier stop on ties; a course with no active member sits at its
first stop; a Mission with no city does not move the ship. A dated course's
ship derives from its dates, so the pass leaves it alone.

The write is sent as the space's strongest local instructor at power level
100, as set-course-plan sends (no token, no join, nobody promoted); a space
with no local instructor is skipped and logged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, cast

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
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.analytics_forward.consent import profile_analytics_rooms
from synapse_pangea_chat.email_invite.course_missions import (
    COURSE_MISSIONS_STATE_EVENT_TYPE,
)
from synapse_pangea_chat.moderation.compat import (
    background_process_args,
    looping_call_interval,
)
from synapse_pangea_chat.public_courses.get_public_courses import (
    DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
    extract_l2,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.instructor_access import joined_local_instructors

logger = logging.getLogger("synapse.module.synapse_pangea_chat.course_trip.position")

COURSE_POSITION_STATE_EVENT_TYPE = "pangea.course_position"
COURSE_PROGRESS_STATE_EVENT_TYPE = "pangea.course_progress"
TRIP_VERSION = 2


def _parse_iso_ms(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:  # silent-ok: an unparsable computed_at reads as never computed
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def is_undated_trip(copy: Optional[Mapping[str, Any]]) -> bool:
    """Whether a course copy runs by progress: version 2 with no start date."""
    return (
        isinstance(copy, Mapping)
        and copy.get("version") == TRIP_VERSION
        and not copy.get("start_date")
        and isinstance(copy.get("missions"), list)
    )


def member_stop(
    missions: Sequence[Mapping[str, Any]],
    progress: Optional[Mapping[str, Any]],
    now_ms: int,
    active_window_ms: int,
) -> Optional[int]:
    """The stop an active member is at: the index of their first incomplete
    Mission (the last when every Mission is complete). None for a member with
    no progress record, or one written before the active window: not active
    this week, not counted."""
    if not isinstance(progress, Mapping):
        return None
    computed = _parse_iso_ms(progress.get("computed_at"))
    if computed is None or now_ms - computed > active_window_ms:
        return None
    can_dos = progress.get("can_dos")
    if not isinstance(can_dos, Mapping):
        return None
    for index, mission in enumerate(missions):
        entry = can_dos.get(mission.get("lo_id"))
        if not (isinstance(entry, Mapping) and entry.get("completed") is True):
            return index
    return len(missions) - 1 if missions else None


def busiest_stop(stops: Sequence[int]) -> int:
    """The stop most active members are at; the earlier stop on ties; the
    first stop when nobody is active."""
    if not stops:
        return 0
    counts: Dict[int, int] = {}
    for stop in stops:
        counts[stop] = counts.get(stop, 0) + 1
    best = min(counts)
    for stop in sorted(counts):
        if counts[stop] > counts[best]:
            best = stop
    return best


class Position(NamedTuple):
    stop: int
    from_stop: int
    since: int

    def content(self) -> Dict[str, Any]:
        return {"stop": self.stop, "from_stop": self.from_stop, "since": self.since}


def parse_position(content: Any) -> Optional[Position]:
    if not isinstance(content, Mapping):
        return None
    values: List[int] = []
    for key in ("stop", "from_stop", "since"):
        value = content.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values.append(value)
    return Position(values[0], values[1], values[2])


def next_position(
    previous: Optional[Position],
    busiest: int,
    missions: Sequence[Mapping[str, Any]],
    now_ms: int,
) -> Optional[Position]:
    """The position to write, or None when nothing changes. The ship sails to
    the busiest stop from where it is; a Mission with no city does not move
    it; the first write has ``from_stop`` equal to ``stop``."""
    has_city = 0 <= busiest < len(missions) and bool(missions[busiest].get("city"))
    if previous is None:
        return Position(busiest, busiest, now_ms)
    if busiest == previous.stop or not has_city:
        return None
    return Position(busiest, previous.stop, now_ms)


class CourseStop(NamedTuple):
    """What one course's pass found, for the log and the operator endpoint."""

    room_id: str
    outcome: str
    position: Optional[Position] = None
    active_members: int = 0
    sender: Optional[str] = None


NOT_A_COURSE = "not_a_course"
NOT_AN_UNDATED_TRIP = "not_an_undated_trip"
NO_INSTRUCTOR = "no_local_instructor"
UNCHANGED = "unchanged"
WRITTEN = "written"
WOULD_WRITE = "would_write"
REFUSED = "refused"


class CoursePositionPass:
    def __init__(self, api: ModuleApi, config: Any) -> None:
        self._api = api
        self._store: Any = api._hs.get_datastores().main
        self._clock = api._hs.get_clock()
        self._plan_type = (
            getattr(config, "course_plan_state_event_type", None)
            or DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE
        )
        self._interval = int(getattr(config, "course_position_interval_seconds", 900))
        self._window_ms = (
            int(getattr(config, "course_position_active_window_seconds", 7 * 86400))
            * 1000
        )
        self._running = False

    def start(self) -> None:
        self._clock.looping_call(
            cast(Any, run_as_background_process),
            cast(Any, looping_call_interval(self._interval)),
            *background_process_args(
                self._api._hs, "pangea_course_position", self.run_all
            ),
        )

    async def course_room_ids(self) -> List[str]:
        """Every room holding a current course plan: the course spaces."""
        rows = await self._store.db_pool.execute(
            "course_position_course_rooms",
            "SELECT DISTINCT room_id FROM current_state_events "
            "WHERE type = ? AND state_key = ''",
            self._plan_type,
        )
        return [row[0] for row in rows]

    async def run_all(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            rooms = await self.course_room_ids()
        except Exception:
            logger.exception("course_position: listing course spaces failed")
            self._running = False
            return
        outcomes: Dict[str, int] = {}
        try:
            for room_id in rooms:
                try:
                    found = await self.run_course(room_id, dry_run=False)
                except Exception:
                    logger.exception("course_position: %s failed", room_id)
                    found = CourseStop(room_id, REFUSED)
                outcomes[found.outcome] = outcomes.get(found.outcome, 0) + 1
        finally:
            self._running = False
        logger.info(
            "course_position: pass over %d course spaces: %s", len(rooms), outcomes
        )

    async def _member_stops(
        self,
        room_id: str,
        state: Mapping[Any, Any],
        missions: Sequence[Mapping[str, Any]],
    ) -> List[int]:
        plan = state.get((self._plan_type, ""))
        language = extract_l2(dict(plan.content)) if plan is not None else None
        members = [
            uid
            for uid in await self._store.get_users_in_room(room_id)
            if self._api.is_mine(uid)
        ]
        profiles = await self._store.get_profile_data_for_users(members)
        now = self._clock.time_msec()
        stops: List[int] = []
        for uid in members:
            for analytics_room in profile_analytics_rooms(
                profiles.get(uid), language or ""
            ):
                progress_state = await self._api.get_room_state(
                    analytics_room, [(COURSE_PROGRESS_STATE_EVENT_TYPE, room_id)]
                )
                record = progress_state.get((COURSE_PROGRESS_STATE_EVENT_TYPE, room_id))
                stop = member_stop(
                    missions,
                    dict(record.content) if record is not None else None,
                    now,
                    self._window_ms,
                )
                if stop is not None:
                    stops.append(stop)
                    break
        return stops

    async def run_course(self, room_id: str, *, dry_run: bool) -> CourseStop:
        state = await self._api.get_room_state(room_id)
        if state.get((self._plan_type, "")) is None:
            return CourseStop(room_id, NOT_A_COURSE)
        copy_event = state.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
        copy = dict(copy_event.content) if copy_event is not None else None
        if not is_undated_trip(copy):
            return CourseStop(room_id, NOT_AN_UNDATED_TRIP)
        missions = [m for m in copy["missions"] if isinstance(m, Mapping)]  # type: ignore[index]
        stops = await self._member_stops(room_id, state, missions)
        current_event = state.get((COURSE_POSITION_STATE_EVENT_TYPE, ""))
        previous = (
            parse_position(dict(current_event.content)) if current_event else None
        )
        position = next_position(
            previous, busiest_stop(stops), missions, self._clock.time_msec()
        )
        if position is None:
            return CourseStop(room_id, UNCHANGED, previous, len(stops))
        instructors = joined_local_instructors(self._api, state)
        if not instructors:
            logger.warning(
                "course_position: no local instructor in %s can send the position",
                room_id,
            )
            return CourseStop(room_id, NO_INSTRUCTOR, position, len(stops))
        sender = instructors[0]
        if dry_run:
            return CourseStop(room_id, WOULD_WRITE, position, len(stops), sender)
        await self._api.create_and_send_event_into_room(
            {
                "type": COURSE_POSITION_STATE_EVENT_TYPE,
                "state_key": "",
                "room_id": room_id,
                "sender": sender,
                "content": position.content(),
            }
        )
        logger.info(
            "course_position: room=%s stop %s -> %s sent_as=%s",
            room_id,
            previous.stop if previous else None,
            position.stop,
            sender,
        )
        return CourseStop(room_id, WRITTEN, position, len(stops), sender)


class CoursePositionRun(Resource):
    """``POST /_synapse/client/pangea/v1/course_position`` (server admins):
    run the position pass for one course now, or see what it would write."""

    isLeaf = True

    def __init__(self, api: ModuleApi, course_position: CoursePositionPass) -> None:
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()
        self._pass = course_position

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
            found = await self._pass.run_course(room_id, dry_run=dry_run)
            if found.outcome == NOT_A_COURSE:
                respond_with_json(
                    request, 404, {"error": "Room has no course plan"}, send_cors=True
                )
                return
            logger.info(
                "course_position: operator=%s room=%s outcome=%s dry_run=%s",
                operator,
                room_id,
                found.outcome,
                dry_run,
            )
            respond_with_json(
                request,
                200,
                {
                    "room_id": room_id,
                    "outcome": found.outcome,
                    "position": found.position.content() if found.position else None,
                    "active_members": found.active_members,
                    "sender": found.sender,
                    "dry_run": dry_run,
                },
                send_cors=True,
            )
        except Exception:
            logger.exception("Error running the course position pass")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )
