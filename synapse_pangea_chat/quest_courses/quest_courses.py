"""Server-admin reads of which course spaces and unclaimed courses use each
quest, for the CMS content review's own copy.

Design: quest-courses.instructions.md.

- ``GET  /_synapse/client/pangea/v1/quest_courses/space_changes?since=``:
  spaces whose course plan changed after a cursor, or ``resync`` when there
  is no cursor or it no longer fits.
- ``GET  /_synapse/client/pangea/v1/quest_courses/spaces?after=``: every
  course space, by room id, for a start-over.
- ``POST /_synapse/client/pangea/v1/quest_courses/rooms``: name and joined
  people for a batch of course rooms.
- ``GET  /_synapse/client/pangea/v1/quest_courses/unclaimed``: every course
  no teacher has claimed yet.

A space's quest is read with the catalog's own field and state-key
precedence (``public_courses``), over every room rather than the public
directory only.
"""

from __future__ import annotations

import json
import logging
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

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
from synapse.storage.database import make_in_list_sql_clause
from twisted.web.resource import Resource

from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.public_courses.get_public_courses import (
    _L2_SQL,
    _PLAN_ID_SQL,
    DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.quest_courses")

PATH = "/_synapse/client/pangea/v1/quest_courses/"

# A claim building for longer than this has failed (quest-courses doc).
STUCK_AFTER_MS = 60 * 60 * 1000

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
MAX_ROOM_IDS = 500


# --------------------------------------------------------------------------
# Pure decisions
# --------------------------------------------------------------------------


# Stream positions read per call at most. Course-plan changes are rare and no
# index covers the change type, so a call after a long gap would otherwise
# scan every change since; capped, each call is bounded and the caller
# continues from where it stopped.
CHANGE_WINDOW = 100_000


def format_cursor(server_name: str, position: int) -> str:
    """A cursor names its server, so one sent to another server is refused
    rather than read as a position there. Server names have no ``/``."""
    return f"{server_name}/{position}"


def parse_cursor(value: Optional[str]) -> Optional[Tuple[str, int]]:
    """A cursor as sent back by a caller, or None when it is not one."""
    if value is None or "/" not in value:
        return None
    server, _, position = value.rpartition("/")
    if not server or not position.isdigit():
        return None
    return server, int(position)


def cursor_is_current(
    cursor: int, written_up_to: int, state_written_before_oldest_kept: bool
) -> bool:
    """Whether changes can be followed from ``cursor`` without a gap.

    A cursor ahead of what the server has written came from another server or
    from before a restore. A cursor older than the oldest change still kept is
    only stale when state was written between the two, since that state's
    changes are gone; with none written (a server whose record was still
    empty), nothing was lost.
    """
    if cursor > written_up_to:
        return False
    return not state_written_before_oldest_kept


class ChangePage(NamedTuple):
    room_ids: List[str]
    cursor: int
    caught_up: bool
    # A single stream position with more course-plan changes than one page:
    # the caller reads that position whole, then continues after it.
    read_whole: Optional[int]


def page_changes(rows: Sequence[Tuple[int, str]], limit: int, upto: int) -> ChangePage:
    """One page of changes from up to ``limit + 1`` rows in stream order.

    A page never ends in the middle of a stream position: the cursor means
    "everything up to here", so splitting one would skip the rest of it.
    """
    if len(rows) <= limit:
        return ChangePage(_unique(r for _, r in rows), upto, True, None)
    boundary = rows[limit][0]
    kept = [room for stream_id, room in rows if stream_id < boundary]
    if not kept:
        return ChangePage([], boundary, False, boundary)
    # Positions between the last kept change and the boundary hold no
    # course-plan change, or they would be in ``rows``.
    return ChangePage(_unique(kept), boundary - 1, False, None)


def _unique(room_ids: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(room_ids))


def is_stuck(status: str, claim_started_at_ms: Optional[int], now_ms: int) -> bool:
    """Whether a claim has been building for longer than a build can take.

    A claim with no start time began before the start was recorded, so it is
    older than the limit.
    """
    if status != "provisioning":
        return False
    if claim_started_at_ms is None:
        return True
    return now_ms - claim_started_at_ms > STUCK_AFTER_MS


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value != "" else None


def unclaimed_invitation(
    invitation_id: str,
    specification: Mapping[str, Any],
    status: str,
    created_at_ms: int,
    claim_started_at_ms: Optional[int],
    now_ms: int,
) -> Dict[str, Any]:
    return {
        "kind": "invitation",
        "invitation_id": invitation_id,
        "room_id": None,
        "quest_id": _text(specification.get("course_plan_id")),
        "l2": _text(specification.get("target_language")),
        "title": _text(specification.get("title")),
        "created_at_ms": created_at_ms,
        "claim_started_at_ms": claim_started_at_ms,
        "stuck": is_stuck(status, claim_started_at_ms, now_ms),
    }


def unclaimed_room(
    room_id: str, plan: "Plan", name: Optional[str], created_at_ms: int
) -> Dict[str, Any]:
    return {
        "kind": "course_room",
        "invitation_id": None,
        "room_id": room_id,
        "quest_id": plan.quest_id,
        "l2": plan.l2,
        "title": name,
        "created_at_ms": created_at_ms,
        "claim_started_at_ms": None,
        "stuck": False,
    }


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


class Plan(NamedTuple):
    quest_id: Optional[str]
    l2: Optional[str]


def _plans_sql(where: str) -> str:
    """One row per room: its own (empty) state key first, else the lowest,
    the catalog's precedence. The empty key sorts before every other, so the
    order is plain ``(room_id, state_key)``: the primary key's own order with
    the type fixed, which lets a paged read stop at its LIMIT instead of
    sorting every row first."""
    return f"""
        SELECT DISTINCT ON (cse.room_id)
            cse.room_id, {_PLAN_ID_SQL} AS plan_id, {_L2_SQL} AS l2
        FROM current_state_events cse
        INNER JOIN event_json ej ON ej.event_id = cse.event_id
        WHERE cse.type = ? AND {where}
        ORDER BY cse.room_id, cse.state_key
    """


class QuestCoursesStore:
    def __init__(
        self,
        api: ModuleApi,
        claims: CourseClaimStore,
        event_type: str,
        bot_user_id: Optional[str],
    ) -> None:
        self._main = api._hs.get_datastores().main
        self._db = self._main.db_pool
        self._claims = claims
        self._event_type = event_type
        self._bot = bot_user_id

    def _in(self, column: str, values: Sequence[str]) -> Tuple[str, List[Any]]:
        clause, args = make_in_list_sql_clause(self._db.engine, column, values)
        return clause, list(args)

    def written_up_to(self) -> int:
        """How far Synapse has written every change: the position its own
        followers (room statistics, the user directory) read up to."""
        return self._main.get_room_max_stream_ordering()

    async def state_lost_before(self, cursor: int) -> bool:
        """Whether state was written after ``cursor`` but before the oldest
        change still kept, so its changes are gone."""
        rows = await self._db.execute(
            "quest_courses_oldest_kept",
            "SELECT MIN(stream_id) FROM current_state_delta_stream",
        )
        oldest = rows[0][0] if rows else None
        if oldest is None or cursor >= int(oldest):
            return False
        lost = await self._db.execute(
            "quest_courses_state_in_gap",
            """
            SELECT 1 FROM events
            WHERE stream_ordering > ? AND stream_ordering < ?
                AND state_key IS NOT NULL
            LIMIT 1
            """,
            cursor,
            int(oldest),
        )
        return bool(lost)

    async def changes(self, since: int, upto: int, limit: int) -> ChangePage:
        rows = await self._db.execute(
            "quest_courses_changes",
            """
            SELECT stream_id, room_id FROM current_state_delta_stream
            WHERE stream_id > ? AND stream_id <= ? AND type = ?
            ORDER BY stream_id
            LIMIT ?
            """,
            since,
            upto,
            self._event_type,
            limit + 1,
        )
        page = page_changes([(int(s), r) for s, r in rows], limit, upto)
        if page.read_whole is None:
            return page
        whole = await self._db.execute(
            "quest_courses_changes_at",
            """
            SELECT room_id FROM current_state_delta_stream
            WHERE stream_id = ? AND type = ?
            """,
            page.read_whole,
            self._event_type,
        )
        return page._replace(room_ids=_unique(r for (r,) in whole))

    async def plans(self, room_ids: Sequence[str]) -> Dict[str, Plan]:
        """The current plan of each room that has one; a room with no plan
        event at all is absent."""
        if not room_ids:
            return {}
        clause, args = self._in("cse.room_id", room_ids)
        rows = await self._db.execute(
            "quest_courses_plans",
            _plans_sql(clause),
            self._event_type,
            *args,
        )
        return {room: Plan(plan_id, l2) for room, plan_id, l2 in rows}

    async def all_spaces(self, after: str, limit: int) -> List[Tuple[str, Plan]]:
        """Up to ``limit`` rooms with a course-plan event after ``after``, some
        of them with no quest. The page is limited on rooms read, not on
        courses found, so it stops where it should; the caller drops the rooms
        with no quest and continues from the last room read."""
        rows = await self._db.execute(
            "quest_courses_all_spaces",
            _plans_sql("cse.room_id > ?") + " LIMIT ?",
            self._event_type,
            after,
            limit,
        )
        return [(room, Plan(plan_id, l2)) for room, plan_id, l2 in rows]

    async def names(self, room_ids: Sequence[str]) -> Dict[str, Optional[str]]:
        if not room_ids:
            return {}
        clause, args = self._in("cse.room_id", room_ids)
        rows = await self._db.execute(
            "quest_courses_names",
            f"""
            SELECT cse.room_id,
                NULLIF(json_extract_path_text(ej.json::json, 'content', 'name'), '')
            FROM current_state_events cse
            INNER JOIN event_json ej ON ej.event_id = cse.event_id
            WHERE cse.type = 'm.room.name' AND cse.state_key = '' AND {clause}
            """,
            *args,
        )
        return {str(room): name for room, name in rows}

    async def joined_people(self, room_ids: Sequence[str]) -> Dict[str, int]:
        """Joined members of each room, leaving out the bot, which is not a
        person in the course."""
        if not room_ids:
            return {}
        clause, args = self._in("room_id", room_ids)
        bot_clause = ""
        if self._bot is not None:
            bot_clause = "AND state_key <> ?"
            args.append(self._bot)
        rows = await self._db.execute(
            "quest_courses_joined",
            f"""
            SELECT room_id, COUNT(*) FROM current_state_events
            WHERE type = 'm.room.member' AND membership = 'join'
                AND {clause} {bot_clause}
            GROUP BY room_id
            """,
            *args,
        )
        counts = {room: 0 for room in room_ids}
        counts.update({room: int(count) for room, count in rows})
        return counts

    async def unclaimed(self, now_ms: int) -> List[Dict[str, Any]]:
        # Both tables are made on first use by their own stores.
        await self._claims.invitations.ensure()
        await self._claims._ensure_table()
        invitations = await self._db.execute(
            "quest_courses_unclaimed_invitations",
            """
            SELECT invitation_id, specification, status, created_at_ms,
                claim_started_at_ms
            FROM pangea_course_invitation
            WHERE room_id IS NULL AND status IN ('prepared', 'provisioning')
            ORDER BY created_at_ms, invitation_id
            """,
        )
        courses = []
        for invitation_id, spec, status, created, started in invitations:
            specification = _specification(invitation_id, spec)
            courses.append(
                unclaimed_invitation(
                    invitation_id, specification, status, created, started, now_ms
                )
            )

        # Older courses were created with their space at once, held by the
        # bot until a teacher claims them.
        held = await self._db.execute(
            "quest_courses_unclaimed_rooms",
            """
            SELECT room_id, created_at_ms FROM pangea_course_claim
            WHERE claimed_by IS NULL
            ORDER BY created_at_ms, room_id
            """,
        )
        rooms = [room for room, _ in held]
        plans = await self.plans(rooms)
        names = await self.names(rooms)
        for room, created in held:
            plan = plans.get(room)
            # A deleted course has no current plan left.
            if plan is None or plan.quest_id is None:
                continue
            courses.append(unclaimed_room(room, plan, names.get(room), created))
        return courses


def _specification(invitation_id: str, value: Any) -> Dict[str, Any]:
    try:
        loaded = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        logger.error(
            "quest_courses: invitation %s has an unreadable specification",
            invitation_id,
        )
        return {}
    if not isinstance(loaded, dict):
        logger.error(
            "quest_courses: invitation %s has a specification that is not an object",
            invitation_id,
        )
        return {}
    return loaded


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class _BadRequest(Exception):
    pass


class _WrongServer(Exception):
    pass


def _limit(request: SynapseRequest) -> int:
    raw = _arg(request, "limit")
    if raw is None:
        return DEFAULT_LIMIT
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_LIMIT:
        raise _BadRequest(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return int(raw)


def _arg(request: SynapseRequest, name: str) -> Optional[str]:
    args: Dict[bytes, list] = dict(request.args or {})
    values = args.get(name.encode())
    if not values:
        return None
    return values[0].decode("utf-8", "replace")


def _space(room_id: str, plan: Optional[Plan]) -> Dict[str, Any]:
    return {
        "room_id": room_id,
        "quest_id": plan.quest_id if plan else None,
        "l2": plan.l2 if plan and plan.quest_id else None,
    }


class QuestCoursesResource(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, store: QuestCoursesStore, mode: str) -> None:
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()
        self._store = store
        self._mode = mode

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._handle, request, "GET")
        return server.NOT_DONE_YET

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._handle, request, "POST")
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
            logger.info("quest_courses: authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
            return
        try:
            if not await self._api.is_user_admin(requester.user.to_string()):
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return
            expected = "POST" if self._mode == "rooms" else "GET"
            if method != expected:
                respond_with_json(
                    request, 405, {"error": "Method not allowed"}, send_cors=True
                )
                return
            body = await getattr(self, "_" + self._mode)(request)
            respond_with_json(request, 200, body, send_cors=True)
        # silent-ok: the caller's malformed request, answered 400 with the reason
        except _BadRequest as e:
            respond_with_json(request, 400, {"error": str(e)}, send_cors=True)
        # silent-ok: a cursor from another server, refused 409 with the reason
        except _WrongServer as e:
            logger.warning("quest_courses: %s", e)
            respond_with_json(
                request,
                409,
                {"error": str(e), "errcode": "ORG.PANGEA.WRONG_SERVER"},
                send_cors=True,
            )
        except Exception:
            logger.exception("quest_courses: %s read failed", self._mode)
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def _space_changes(self, request: SynapseRequest) -> Dict[str, Any]:
        raw = _arg(request, "since")
        limit = _limit(request)
        written_up_to = self._store.written_up_to()
        here = self._api.server_name
        if raw is None:
            return {"resync": True, "cursor": format_cursor(here, written_up_to)}
        parsed = parse_cursor(raw)
        if parsed is None:
            raise _BadRequest("since must be a cursor this endpoint returned")
        server, since = parsed
        if server != here:
            raise _WrongServer(
                f"since is a cursor from {server}, not from this server ({here})"
            )
        lost = since <= written_up_to and await self._store.state_lost_before(since)
        if not cursor_is_current(since, written_up_to, lost):
            logger.warning(
                "quest_courses: cursor %s does not fit (written up to %s, "
                "changes lost: %s); the caller starts over",
                since,
                written_up_to,
                lost,
            )
            return {"resync": True, "cursor": format_cursor(here, written_up_to)}
        upto = min(written_up_to, since + CHANGE_WINDOW)
        page = await self._store.changes(since, upto, limit)
        plans = await self._store.plans(page.room_ids)
        return {
            "resync": False,
            "spaces": [_space(room, plans.get(room)) for room in page.room_ids],
            "cursor": format_cursor(here, page.cursor),
            "caught_up": page.caught_up and upto == written_up_to,
        }

    async def _spaces(self, request: SynapseRequest) -> Dict[str, Any]:
        limit = _limit(request)
        after = _arg(request, "after") or ""
        rows = await self._store.all_spaces(after, limit)
        return {
            "spaces": [_space(room, plan) for room, plan in rows if plan.quest_id],
            "next_after": rows[-1][0] if len(rows) == limit else None,
        }

    async def _rooms(self, request: SynapseRequest) -> Dict[str, Any]:
        body = await extract_body_json(request)
        room_ids = body.get("room_ids") if isinstance(body, dict) else None
        if not isinstance(room_ids, list) or not all(
            isinstance(r, str) for r in room_ids
        ):
            raise _BadRequest("room_ids must be a list of room ids")
        if len(room_ids) > MAX_ROOM_IDS:
            raise _BadRequest(f"At most {MAX_ROOM_IDS} room ids per call")
        room_ids = _unique(room_ids)
        plans = await self._store.plans(room_ids)
        # Only a room with a current course plan is answered for, so this read
        # cannot be used to look up other rooms.
        courses = [r for r in room_ids if r in plans and plans[r].quest_id]
        names = await self._store.names(courses)
        people = await self._store.joined_people(courses)
        return {
            "rooms": [
                (
                    {
                        "room_id": room,
                        "course": True,
                        "name": names.get(room),
                        "joined_members": people[room],
                    }
                    if room in people
                    else {
                        "room_id": room,
                        "course": False,
                        "name": None,
                        "joined_members": None,
                    }
                )
                for room in room_ids
            ]
        }

    async def _unclaimed(self, request: SynapseRequest) -> Dict[str, Any]:
        return {"courses": await self._store.unclaimed(int(time.time() * 1000))}


def register_quest_courses(
    api: ModuleApi, config: "PangeaChatConfig", claims: CourseClaimStore
) -> QuestCoursesStore:
    if config.quest_courses_bot_user_id is None:
        logger.warning(
            "quest_courses: quest_courses_bot_user_id is not set, so the bot is "
            "counted as a member of every course it is in"
        )
    store = QuestCoursesStore(
        api,
        claims,
        config.course_plan_state_event_type or DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
        config.quest_courses_bot_user_id,
    )
    for mode in ("space_changes", "spaces", "rooms", "unclaimed"):
        api.register_web_resource(
            path=PATH + mode, resource=QuestCoursesResource(api, store, mode)
        )
    return store
