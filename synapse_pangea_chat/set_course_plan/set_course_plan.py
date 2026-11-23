"""Server-admin endpoint: point an existing course space at a different quest,
give it the new quest's Can-Do list, and rename it to match.

Design: set-course-plan.instructions.md. This docstring owns the mechanics.

``POST /_synapse/client/pangea/v1/set_course_plan``, server admins only (401
without a valid token, 403 for a non-admin, 400 for a malformed body).

Request: ``room_id``, ``quest_id`` (the quest to point at),
``expected_quest_id`` (the quest the operator expects the space holds now),
``title``, ``description``, optional ``dry_run``. One space per call; the
operator script loops over spaces.

Response: ``previous_quest_id``, ``quest_id``, ``previous_name``,
``previous_topic``, ``changes`` (the event types written, in order),
``can_do_list`` (``built``, ``previous_revision``, ``revision``,
``missions``, ``problems``: each kind of thing the list could not carry over
from the quest, with its count of entries; null when no list was built or
tried), ``sender``, ``changed``, ``dry_run``. When the list cannot be built,
the request is refused with ``error`` and ``code`` (course_missions failure
codes, or ``ranked_unfinished`` when the CMS was too slow to rank every
Can-Do): 422 when the quest is gone or malformed, 502 when the CMS failed.

Steps:

1. Read ``pangea.course_plan``. No plan: 404, not a course.
2. Read the plan id as public-courses does (``extract_plan_id``). A plan id
   other than ``expected_quest_id``: 409 with ``current_quest_id``. Checked
   before "already there".
3. Plan, name and topic already match and the course copy has content:
   ``changed: false``, nothing written. A space on the quest whose copy is
   empty or missing gets it built (admin-dash-api#472, scripts/
   write_course_copies.py).
4. On a plan change, build ``pangea.course_missions`` for the new quest with
   ``CourseMissionsReader`` (the read creation uses; the quest owner's private
   activities and the space's pins are not in it), at revision one above the
   old copy's (1 if none) so an open Studio refuses a stale publish. Re-read
   the plan and copy after the read; either changed: 409, nothing written. A
   read that fails refuses the whole request with its code and writes
   nothing: an empty list would put the shared quest in place of the course's
   own, the silent loss this replaced. The failure is captured.
5. Write the plan with ``uuid`` set to the new quest. ``l2`` and every other
   field are kept and the request cannot change them; the legacy
   ``course_plan_id`` is dropped.
6. Write the name (``title``) and topic (``description``, in createRoom's
   shape), each only when it differs.
7. Send every event as ``joined_local_instructors(...)[0]`` (power level 100).
   None: 409, nothing written.
8. Write order is copy, plan, name, topic. A room refusal (AuthError): 409
   with ``refused`` and ``written``; any other failure: 500 with ``written``.
   Stopped before the plan landed: resend the same request. Stopped after:
   resend with the new quest as ``expected_quest_id``.
9. Log the operator, room, old and new quest, changes and sender.

Replacing a list the server built wrongly (``replace_copy_event_id``): the
course stays on its quest (``quest_id`` equals ``expected_quest_id``) and its
list is rebuilt even though it has content, but only while the space still
holds the very list event named. Any other list, or none, is refused with 409
and ``current_copy_event_id`` before the CMS read, so a list a teacher has
published since is never overwritten; one published during the read is refused
by the re-read. The answer counts the activities in the current and rebuilt
lists, and the current ones outside the quest's selection. Used by
``scripts/write_course_copies.py --rooms`` (pangeachat/.github#832: lists the
builder made from the newest 200 rows only, before it read selections by id).

A dry run does everything but the writes, including the CMS read.
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
    NO_QUEST,
    QUEST_MALFORMED,
    QUEST_NOT_FOUND,
    RANKED_UNFINISHED,
    CourseMissionsError,
    CourseMissionsReader,
    capture_read_failure,
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


def can_do_list_revision(content: Optional[Mapping[str, Any]]) -> Optional[int]:
    """A Can-Do list's revision, or None when there is no readable list."""
    revision = content.get("revision") if content is not None else None
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
        return revision
    return None


# A read failure the request itself caused, rather than the CMS.
_QUEST_FAILURES = frozenset({NO_QUEST, QUEST_NOT_FOUND, QUEST_MALFORMED})


def can_do_list_content(
    built: Mapping[str, Any], current: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """The new quest's Can-Do list at one revision above the current list's (1
    when there is none), so a Studio opened on the old list refuses its
    publish."""
    return {**built, "revision": (can_do_list_revision(current) or 0) + 1}


def _content(event: Any) -> Optional[Dict[str, Any]]:
    if event is None or not isinstance(event.content, Mapping):
        return None
    return dict(event.content)


def topic_content(text: str) -> Dict[str, Any]:
    """Topic content in the shape Synapse's own createRoom writes."""
    return {"topic": text, "m.topic": {"m.text": [{"body": text}]}}


def planned_writes(
    update: PlanUpdate,
    plan_event_type: str,
    can_do_list: Optional[Dict[str, Any]],
    current_name: Optional[str],
    current_topic: Optional[str],
    title: str,
    description: str,
) -> List[Tuple[str, Dict[str, Any]]]:
    """The state events to send, in order: the course space's Can-Do list when
    one is given, the course plan when it changes, then the name, then the
    topic, each only when it differs from what the space has.

    ``can_do_list`` is the Can-Do list to write, or None to leave the
    space's list alone. A plan change always carries one. Once the plan points
    at the new quest, only an empty list (left by a failed read) is given, so a
    re-run does not wipe edits the teacher has published since. The list goes
    first so a refusal part-way leaves the plan on the old quest."""
    writes: List[Tuple[str, Dict[str, Any]]] = []
    if update.outcome == UPDATE and can_do_list is None:
        raise ValueError("a plan change needs the new quest's Can-Do list")
    if can_do_list is not None:
        writes.append((COURSE_MISSIONS_STATE_EVENT_TYPE, can_do_list))
    if update.outcome == UPDATE and update.content is not None:
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
    # The list event to replace, or None for an ordinary move or build.
    replace_copy_event_id: Optional[str] = None


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
    replace: Optional[str] = None
    if "replace_copy_event_id" in body:
        replace = _valid_id(body.get("replace_copy_event_id"))
        if replace is None:
            return None, "'replace_copy_event_id' must be an event ID"
        if quest_id != expected_quest_id:
            return None, "A replacement keeps the course on its quest"
    return (
        SetCoursePlanRequest(
            room_id, quest_id, expected_quest_id, title, description, dry_run, replace
        ),
        None,
    )


def _activity_count(content: Optional[Mapping[str, Any]]) -> int:
    missions = (content or {}).get("missions")
    return sum(
        len(set(m.get("activity_ids") or []))
        for m in (missions if isinstance(missions, list) else [])
        if isinstance(m, Mapping) and isinstance(m.get("activity_ids"), list)
    )


def outside_selection(
    content: Optional[Mapping[str, Any]], selections: Mapping[str, List[str]]
) -> int:
    """The list's activities under Can-Dos whose quest entry selects some,
    that the selection does not name. An entry selecting nothing counts none."""
    missions = (content or {}).get("missions")
    count = 0
    for m in missions if isinstance(missions, list) else []:
        if not isinstance(m, Mapping) or not isinstance(m.get("activity_ids"), list):
            continue
        lo_id = m.get("lo_id")
        chosen = set(selections.get(lo_id, [])) if isinstance(lo_id, str) else set()
        if chosen:
            count += len(set(m["activity_ids"]) - chosen)
    return count


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
            code, answer = await self.apply(req, operator)
            respond_with_json(request, code, answer, send_cors=True)
        except Exception:
            logger.exception("Error setting a course plan")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def _replacement_counts(
        self,
        quest_id: str,
        event_id: Optional[str],
        current: Optional[Mapping[str, Any]],
        built: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """What a replacement changes, for the operator to read first."""
        try:
            selections = await self._course_missions.quest_selections(quest_id)
            outside: Optional[int] = outside_selection(current, selections)
        # silent-ok: a count for the operator's report, not the write; logged, reported as null
        except Exception as e:
            logger.warning(
                "set_course_plan: the selection of quest %s could not be read "
                "for the report: %s",
                quest_id,
                type(e).__name__,
            )
            outside = None
        return {
            "replaced": event_id,
            "current_activities": _activity_count(current),
            "rebuilt_activities": _activity_count(built) if built else None,
            "current_outside_selection": outside,
        }

    async def apply(
        self, req: SetCoursePlanRequest, operator: str
    ) -> Tuple[int, Dict[str, Any]]:
        """The response for an authorized, parsed request: its HTTP status and
        body. Shared with the invitation-side replacement of a claimed course
        (create-course-space.instructions.md, "Replacing the course plan")."""
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
            return (404, {"error": "Room has no course plan"})
        if update.outcome == MISMATCH:
            return (
                409,
                {
                    "error": "Room points at a different quest than expected",
                    "current_quest_id": update.current_quest_id,
                },
            )

        current_name = _state_text(state.get((EventTypes.Name, "")), "name")
        current_topic = _state_text(state.get((EventTypes.Topic, "")), "topic")
        list_event = state.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
        current_list = _content(list_event)
        list_event_id = getattr(list_event, "event_id", None)
        replacing = req.replace_copy_event_id is not None
        if replacing and list_event_id != req.replace_copy_event_id:
            # A teacher published over the list the operator named: theirs wins.
            return (
                409,
                {
                    "error": "The course's Can-Do list is not the one named",
                    "current_copy_event_id": list_event_id,
                },
            )
        new_list: Optional[Dict[str, Any]] = None
        built: Optional[Dict[str, Any]] = None
        problems: Dict[str, int] = {}
        # A switch builds the new quest's Can-Do list; a space already on the
        # quest whose list is empty (a failed read, before this refused) or
        # missing (made before courses got their own list,
        # admin-dash-api#472) gets it built, and so does a replacement of the
        # named list.
        rebuild = not current_list or replacing
        counts: Dict[str, Any] = {}
        if update.outcome == UPDATE or rebuild:
            try:
                result = await self._course_missions.read(req.quest_id)
            except CourseMissionsError as e:
                logger.error(
                    "set_course_plan: no Can-Do list could be built for quest "
                    "%s in %s (%s); refused, nothing written",
                    req.quest_id,
                    room_id,
                    e,
                )
                capture_read_failure(e, req.quest_id, room_id=room_id)
                return (
                    422 if e.code in _QUEST_FAILURES else 502,
                    {
                        "error": "The quest's Can-Do list could not be built",
                        "code": e.code,
                    },
                )
            if result.unfinished:
                # Partly ranked from the newest rows only: written, it would
                # stay, since a list with content is never rebuilt here.
                logger.error(
                    "set_course_plan: %d Can-Dos of quest %s not read in time "
                    "for %s; refused, nothing written",
                    len(result.unfinished),
                    req.quest_id,
                    room_id,
                )
                return (
                    502,
                    {
                        "error": "The CMS was too slow to build the quest's "
                        "Can-Do list",
                        "code": RANKED_UNFINISHED,
                    },
                )
            built = result.content
            for problem in result.problems:
                problems[problem.code] = problems.get(problem.code, 0) + 1
            if replacing:
                # Every CMS read comes before the re-check below, so nothing
                # waits on the network between it and the write.
                counts = await self._replacement_counts(
                    req.quest_id, req.replace_copy_event_id, current_list, built
                )
            # The reads can take seconds; a teacher who changed course or
            # published a Can-Do list in the meantime wins.
            again = await self._api.get_room_state(
                room_id,
                [
                    (self._event_type, ""),
                    (COURSE_MISSIONS_STATE_EVENT_TYPE, ""),
                ],
            )
            again_list = again.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
            if (
                _content(again.get((self._event_type, ""))) != _content(event)
                or _content(again_list) != current_list
                or getattr(again_list, "event_id", None) != list_event_id
            ):
                logger.warning(
                    "set_course_plan: %s changed while its Can-Do list was "
                    "built; refused, nothing written",
                    room_id,
                )
                return (
                    409,
                    {"error": "The space changed while its Can-Do list was built"},
                )
            new_list = can_do_list_content(result.content, current_list)
        writes = planned_writes(
            update,
            self._event_type,
            new_list,
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
            # Reported whenever a Can-Do list was built or tried, so a failed
            # rebuild of an empty list is visible too.
            "can_do_list": (
                {
                    "built": built is not None,
                    "previous_revision": can_do_list_revision(current_list),
                    "revision": can_do_list_revision(new_list),
                    "missions": len((new_list or {}).get("missions") or []),
                    "problems": problems,
                    **counts,
                }
                if update.outcome == UPDATE or rebuild
                else None
            ),
            "dry_run": req.dry_run,
        }
        if not writes:
            return (200, {**answer, "sender": None, "changed": False})

        # One instructor sends every write: readers ignore a Can-Do list
        # sent below power level 100, so a weaker sender is never used.
        instructors = joined_local_instructors(self._api, state)
        if not instructors:
            logger.warning(
                "set_course_plan: no local instructor in %s can send %s; "
                "refused, nothing written",
                room_id,
                answer["changes"],
            )
            return (409, {"error": "No local instructor can send the course events"})
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
                    return (
                        409,
                        {
                            "error": "The room refused a write",
                            "errcode": e.errcode,
                            "refused": event_type,
                            "written": written,
                        },
                    )
                except Exception:
                    logger.exception(
                        "set_course_plan: writing %s to %s failed; "
                        "already written: %s",
                        event_type,
                        room_id,
                        written,
                    )
                    return (500, {"error": "Internal server error", "written": written})
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
        return (200, {**answer, "sender": sender, "changed": not req.dry_run})
