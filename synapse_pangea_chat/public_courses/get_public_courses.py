"""Public course catalog query.

A room appears in the catalog if, and only if, it is published in the public
room directory and has a *current* ``pangea.course_plan`` state event carrying a
plan id. Nothing else is checked — not member count, not join rule, and not the
contents of the quest.

The plan id is read from ``uuid``, falling back to ``course_plan_id``. The
target language is read from ``l2`` on the same state event, so there is no CMS
call on the read path. Eligibility and the language filter are both applied in
SQL, before pagination, so a page comes back full unless the catalog is
exhausted.
"""

import json
import logging
import time
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from synapse.api.constants import HistoryVisibility
from synapse.storage.databases.main.room import RoomStore

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.public_courses.types import (
    Course,
    CourseFilters,
    PublicCoursesResponse,
)

EventContent = Dict[str, Any]
StateKeyMap = Dict[Optional[str], EventContent]
RoomStateMap = Dict[str, StateKeyMap]
AllRoomsState = Dict[str, RoomStateMap]

# In-memory cache for room preview state: {room_id: (state, timestamp)}
_cache: Dict[str, Tuple[RoomStateMap, float]] = {}
_CACHE_TTL_SECONDS = 60  # 1 minute TTL

# Cached catalog size: {(event_type, base_language or None): (count, timestamp)}
_count_cache: Dict[Tuple[str, Optional[str]], Tuple[int, float]] = {}
_COUNT_CACHE_TTL_SECONDS = 60
# The language half of the key is caller-supplied, so the key space is not
# ours to bound by construction: a caller cycling ?target_language= would grow
# this dict forever *and* miss every time, putting a full catalog COUNT behind
# every request. Cap it and evict the oldest entry once it is full.
_COUNT_CACHE_MAX_ENTRIES = 256

logger = logging.getLogger("synapse_pangea_chat.get_public_courses")

# List of state events required to build a course preview
RESPONSE_STATE_EVENTS: Tuple[str, ...] = (
    "m.room.avatar",
    "m.room.canonical_alias",
    "m.room.create",
    "m.room.join_rules",
    "m.room.name",
    "m.room.power_levels",
    "m.room.topic",
    "pangea.course_plan",
)

DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE = "pangea.course_plan"

DEFAULT_LIMIT = 10

# The plan id is read from these content keys, in this order. Spaces created
# server-side by create_course_space write ``course_plan_id``; everything else
# writes ``uuid``. This tuple is the single definition of that rule: the SQL
# below is generated from it, and anything outside the read path that needs to
# know whether a room is a course imports ``extract_plan_id`` rather than
# restating the keys. Two copies of this rule are how the catalog came to
# disagree with itself.
PLAN_ID_CONTENT_KEYS: Tuple[str, ...] = ("uuid", "course_plan_id")

# The target language, written into the same state event when the quest is
# attached. Empty is the same as absent, here and in SQL.
L2_CONTENT_KEY = "l2"


def extract_plan_id(content: Mapping[str, Any]) -> Optional[str]:
    """The course plan id carried by a ``pangea.course_plan`` content, if any.

    The Python expression of the same rule the catalog query applies in SQL:
    first non-empty value across ``PLAN_ID_CONTENT_KEYS``, empty treated as
    absent, value returned exactly as stored.
    """
    for key in PLAN_ID_CONTENT_KEYS:
        value = content.get(key)
        if isinstance(value, str) and value != "":
            return value
    return None


def extract_l2(content: Mapping[str, Any]) -> Optional[str]:
    """The target language carried by a ``pangea.course_plan`` content, if any."""
    value = content.get(L2_CONTENT_KEY)
    if isinstance(value, str) and value != "":
        return value
    return None


def _json_field_sql(key: str) -> str:
    """``NULLIF(...)`` over one content key of the joined event JSON."""
    return f"NULLIF(json_extract_path_text(ej.json::json, 'content', '{key}'), '')"


# Generated from PLAN_ID_CONTENT_KEYS so the key list and its precedence live
# in exactly one place. The keys are module constants, never caller input.
_PLAN_ID_SQL = "COALESCE({})".format(
    ", ".join(_json_field_sql(key) for key in PLAN_ID_CONTENT_KEYS)
)
_L2_SQL = _json_field_sql(L2_CONTENT_KEY)


class InvalidCatalogParamError(ValueError):
    """A query parameter the catalog cannot honor.

    Raised rather than quietly ignored. The contract has no fall-back path for
    a filter that cannot be served, and the same reasoning covers a cursor:
    silently restarting from the head, or silently dropping a filter, hands
    back a catalog that answers a question the caller did not ask, with
    nothing in the response to say so.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CatalogRow(NamedTuple):
    """One eligible room, as the catalog query returns it."""

    room_id: str
    plan_id: str
    l2: Optional[str]


def base_language(value: Optional[str]) -> Optional[str]:
    """Base-language code: ``es-MX`` -> ``es``. ``None`` for empty input."""
    if not value:
        return None
    base = value.split("-")[0].strip().lower()
    return base or None


def _is_cache_valid(timestamp: float) -> bool:
    """Check if a cache entry is still valid based on TTL."""
    return time.time() - timestamp < _CACHE_TTL_SECONDS


def _get_cached_room(room_id: str) -> Optional[RoomStateMap]:
    """Get cached room data if it exists and is still valid."""
    if room_id in _cache:
        data, timestamp = _cache[room_id]
        if _is_cache_valid(timestamp):
            return data
        del _cache[room_id]
    return None


def _cache_room_data(room_id: str, data: RoomStateMap) -> None:
    """Cache room data with current timestamp."""
    _cache[room_id] = (data, time.time())


def _cleanup_expired_cache() -> None:
    """Remove expired entries from both module caches."""
    current_time = time.time()
    expired_rooms = [
        room_id
        for room_id, (_, timestamp) in _cache.items()
        if current_time - timestamp >= _CACHE_TTL_SECONDS
    ]
    for room_id in expired_rooms:
        del _cache[room_id]

    expired_counts = [
        cache_key
        for cache_key, (_, timestamp) in _count_cache.items()
        if current_time - timestamp >= _COUNT_CACHE_TTL_SECONDS
    ]
    for cache_key in expired_counts:
        del _count_cache[cache_key]


def _get_event_content(
    event_state_map: StateKeyMap,
    preferred_state_keys: Tuple[Optional[str], ...] = (None, ""),
) -> EventContent:
    """Extract the content payload from the first matching state event."""

    for state_key in preferred_state_keys:
        if state_key in event_state_map:
            event_json = event_state_map[state_key]
            if isinstance(event_json, dict):
                content = event_json.get("content")
                if isinstance(content, dict):
                    return content

    for event_json in event_state_map.values():
        if isinstance(event_json, dict):
            content = event_json.get("content")
            if isinstance(content, dict):
                return content

    return {}


def _resolve_event_type(config: PangeaChatConfig) -> str:
    required = getattr(
        config,
        "course_plan_state_event_type",
        DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
    )
    if not isinstance(required, str) or not required:
        return DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE
    return required


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


class Cursor(NamedTuple):
    """Where in the catalog a page starts.

    ``after_room_id`` is a keyset cursor: the last room id of the previous
    page. ``offset`` is the legacy decimal ``since`` value, accepted once for
    compatibility with clients still holding one; every cursor this module
    hands out afterwards is a keyset cursor.
    """

    after_room_id: Optional[str]
    offset: int


def parse_since(since: Optional[str]) -> Cursor:
    """Read a ``since`` value, rejecting anything that is neither cursor form.

    A keyset cursor is a room id, so it starts with ``!``. Treating any other
    string as one is not harmless: room ids sort below nearly every printable
    character, so ``cse.room_id > 'abc'`` matches nothing and the caller gets
    ``200`` with an empty chunk and a null ``next_batch`` — indistinguishable
    from an exhausted catalog. A malformed cursor is a client bug and is told
    so; it must not present as "there are no courses".
    """
    if not since:
        return Cursor(after_room_id=None, offset=0)
    candidate = since.strip()
    if not candidate:
        return Cursor(after_room_id=None, offset=0)
    if candidate.isdigit():
        # Legacy offset cursor from a previously-deployed client.
        return Cursor(after_room_id=None, offset=int(candidate))
    if candidate.startswith("!"):
        return Cursor(after_room_id=candidate, offset=0)
    raise InvalidCatalogParamError(
        "since must be a room id from a previous next_batch, "
        "or a non-negative integer"
    )


# ---------------------------------------------------------------------------
# Catalog query — eligibility + language filter, in one pass, before paging
# ---------------------------------------------------------------------------


def _catalog_from_clause(after_room_id: Optional[str]) -> Tuple[str, List[Any]]:
    """Inner select over current room state; one row per eligible room.

    Only ``current_state_events`` is consulted, so a room that once carried a
    course plan and no longer does is not a course. ``DISTINCT ON`` collapses a
    room that somehow carries several state keys, preferring the empty one.

    Fields are read with ``json``, never ``jsonb``. Casting to ``jsonb``
    normalises the whole document up front, and ``jsonb`` cannot represent a
    ``\\u0000`` escape — so a single event carrying one anywhere in its content
    aborts the query and empties the catalog for every user. ``json`` is stored
    as text and only the extracted path is converted, so a malformed event can
    at worst spoil its own row.
    """
    room_predicate = "AND cse.room_id > ?" if after_room_id else ""
    params: List[Any] = [after_room_id] if after_room_id else []
    sql = f"""
    SELECT DISTINCT ON (cse.room_id)
        cse.room_id AS room_id,
        {_PLAN_ID_SQL} AS plan_id,
        {_L2_SQL} AS l2
    FROM current_state_events cse
    INNER JOIN rooms r ON r.room_id = cse.room_id
    INNER JOIN event_json ej ON ej.event_id = cse.event_id
    WHERE cse.type = ?
      AND r.is_public = TRUE
      {room_predicate}
    ORDER BY cse.room_id, (cse.state_key <> '') ASC, cse.state_key ASC
    """
    return sql, params


def _eligibility_predicate(
    target_base_language: Optional[str],
) -> Tuple[str, List[Any]]:
    """Outer WHERE over the candidate rows."""
    sql = "plan_id IS NOT NULL"
    params: List[Any] = []
    if target_base_language:
        # A room with no l2 has no base language and is excluded when a
        # language filter is passed.
        sql += " AND lower(split_part(l2, '-', 1)) = ?"
        params.append(target_base_language)
    return sql, params


async def _fetch_catalog_page(
    room_store: RoomStore,
    required_course_event_type: str,
    cursor: Cursor,
    limit: int,
    target_base_language: Optional[str],
) -> List[CatalogRow]:
    """Fetch up to *limit* eligible rooms, ordered by room id."""
    inner_sql, inner_params = _catalog_from_clause(cursor.after_room_id)
    where_sql, where_params = _eligibility_predicate(target_base_language)

    sql = f"""
SELECT room_id, plan_id, l2 FROM (
{inner_sql}
) candidates
WHERE {where_sql}
ORDER BY room_id
OFFSET ?
LIMIT ?
    """

    params: List[Any] = [
        required_course_event_type,
        *inner_params,
        *where_params,
        cursor.offset,
        limit,
    ]

    rows = await room_store.db_pool.execute(
        "get_public_courses_catalog_page",
        sql,
        *params,
    )
    return [CatalogRow(room_id=row[0], plan_id=row[1], l2=row[2]) for row in rows or []]


def _store_catalog_count(
    cache_key: Tuple[str, Optional[str]],
    count: int,
) -> None:
    """Cache a catalog size, evicting the oldest entry if the cache is full."""
    if cache_key not in _count_cache and len(_count_cache) >= _COUNT_CACHE_MAX_ENTRIES:
        oldest = min(_count_cache, key=lambda key: _count_cache[key][1])
        del _count_cache[oldest]
    _count_cache[cache_key] = (count, time.time())


async def _count_catalog(
    room_store: RoomStore,
    required_course_event_type: str,
    target_base_language: Optional[str],
) -> int:
    """Size of the (optionally language-filtered) catalog, cached for a minute.

    The count scans every public room's current course-plan state, so it is not
    something to run on every request; callers only need an estimate.
    """
    cache_key = (required_course_event_type, target_base_language)
    cached = _count_cache.get(cache_key)
    if cached is not None and time.time() - cached[1] < _COUNT_CACHE_TTL_SECONDS:
        return cached[0]

    inner_sql, _ = _catalog_from_clause(None)
    where_sql, where_params = _eligibility_predicate(target_base_language)

    sql = f"""
SELECT COUNT(*) FROM (
{inner_sql}
) candidates
WHERE {where_sql}
    """

    rows = await room_store.db_pool.execute(
        "get_public_courses_catalog_count",
        sql,
        required_course_event_type,
        *where_params,
    )

    count = 0
    if rows:
        count = int(rows[0][0])

    _store_catalog_count(cache_key, count)
    return count


def reset_caches() -> None:
    """Drop all module-level caches (used by tests)."""
    _cache.clear()
    _count_cache.clear()


# ---------------------------------------------------------------------------
# Preview state for the rooms on the page
# ---------------------------------------------------------------------------


async def _fetch_room_state(
    room_store: RoomStore,
    room_ids: List[str],
    event_types_with_required: List[str],
) -> Dict[str, RoomStateMap]:
    """Fetch state events for *room_ids*, using the in-memory cache."""
    rooms_data: Dict[str, RoomStateMap] = {}
    rooms_to_fetch: List[str] = []

    for room_id in room_ids:
        cached = _get_cached_room(room_id)
        if cached is not None:
            rooms_data[room_id] = cached
        else:
            rooms_to_fetch.append(room_id)

    if rooms_to_fetch:
        room_placeholders = ",".join(["?" for _ in rooms_to_fetch])
        event_type_placeholders = ",".join(["?" for _ in event_types_with_required])

        state_events_query = f"""
SELECT cse.room_id, cse.type, cse.state_key, ej.json
FROM current_state_events cse
INNER JOIN event_json ej ON ej.event_id = cse.event_id
WHERE cse.room_id IN ({room_placeholders})
  AND cse.type IN ({event_type_placeholders})
        """

        params: Tuple[Any, ...] = (
            *rooms_to_fetch,
            *event_types_with_required,
        )

        rows = await room_store.db_pool.execute(
            "get_public_courses_state_events",
            state_events_query,
            *params,
        )

        fetched_data: AllRoomsState = {}

        for row in rows:
            room_id, event_type, state_key, json_data = row

            room_state = fetched_data.setdefault(room_id, {})

            if isinstance(json_data, str):
                event_data = json.loads(json_data)
            else:
                event_data = json_data

            event_state_map = room_state.setdefault(event_type, {})
            event_state_map[state_key] = event_data

        for room_id in rooms_to_fetch:
            room_state = fetched_data.get(room_id, {})
            rooms_data[room_id] = room_state
            _cache_room_data(room_id, room_state)

    return rooms_data


async def _fetch_room_stats(
    room_store: RoomStore,
    room_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Fetch room stats for *room_ids*."""
    if not room_ids:
        return {}

    room_stats_placeholders = ",".join(["?" for _ in room_ids])
    room_stats_query = f"""
SELECT
    room_id,
    history_visibility,
    guest_access,
    join_rules,
    room_type,
    joined_members
FROM room_stats_state
INNER JOIN room_stats_current USING (room_id)
WHERE room_id IN ({room_stats_placeholders})
    """

    room_stats_rows = await room_store.db_pool.execute(
        "get_public_courses_room_stats",
        room_stats_query,
        *room_ids,
    )

    room_stats: Dict[str, Dict[str, Any]] = {}
    for row in room_stats_rows:
        (
            rid,
            history_visibility,
            guest_access,
            join_rules,
            room_type,
            joined_members,
        ) = row
        room_stats[rid] = {
            "history_visibility": history_visibility,
            "guest_access": guest_access,
            "join_rules": join_rules,
            "room_type": room_type,
            "joined_members": joined_members,
        }
    return room_stats


def _build_course(
    entry: CatalogRow,
    room_data: RoomStateMap,
    stats: Dict[str, Any],
) -> Course:
    """Build a single Course dict from the catalog row plus room state."""
    name = None
    topic = None
    avatar_url = None
    canonical_alias = None

    if "m.room.name" in room_data:
        name = _get_event_content(room_data["m.room.name"]).get("name")
    if "m.room.topic" in room_data:
        topic = _get_event_content(room_data["m.room.topic"]).get("topic")
    if "m.room.avatar" in room_data:
        avatar_url = _get_event_content(room_data["m.room.avatar"]).get("url")
    if "m.room.canonical_alias" in room_data:
        canonical_alias = _get_event_content(room_data["m.room.canonical_alias"]).get(
            "alias"
        )

    history_visibility = stats.get("history_visibility")
    guest_access = stats.get("guest_access")
    join_rules = stats.get("join_rules")
    room_type = stats.get("room_type")
    joined_members = stats.get("joined_members", 0)

    return Course(
        room_id=entry.room_id,
        name=name,
        topic=topic,
        avatar_url=avatar_url,
        canonical_alias=canonical_alias,
        course_id=entry.plan_id,
        num_joined_members=joined_members,
        world_readable=history_visibility == HistoryVisibility.WORLD_READABLE,
        guest_can_join=guest_access == "can_join",
        join_rule=join_rules,
        room_type=room_type,
        target_language=entry.l2,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def get_public_courses(
    room_store: RoomStore,
    config: PangeaChatConfig,
    limit: int,
    since: Optional[str],
    filters: Optional[CourseFilters] = None,
) -> PublicCoursesResponse:
    _cleanup_expired_cache()

    required_course_event_type = _resolve_event_type(config)

    event_types_with_required = list(
        dict.fromkeys(RESPONSE_STATE_EVENTS + (required_course_event_type,))
    )

    if limit <= 0:
        limit = DEFAULT_LIMIT

    cursor = parse_since(since)

    requested_language = (filters or {}).get("target_language")
    target_base_language = base_language(requested_language)
    if requested_language and target_base_language is None:
        # e.g. ?target_language=- . Carrying on would drop the predicate and
        # serve the whole unfiltered catalog, which is the silent fall-back the
        # contract abolishes: a filter that cannot be honored is refused, not
        # widened.
        raise InvalidCatalogParamError(
            "target_language must contain a language code, e.g. es or es-MX"
        )

    total_room_count = await _count_catalog(
        room_store, required_course_event_type, target_base_language
    )

    # Overfetch by one so a non-null next_batch means more results genuinely exist.
    entries = await _fetch_catalog_page(
        room_store,
        required_course_event_type,
        cursor,
        limit + 1,
        target_base_language,
    )

    has_next = len(entries) > limit
    page = entries[:limit]

    if not page:
        return PublicCoursesResponse(
            chunk=[],
            next_batch=None,
            total_room_count_estimate=total_room_count,
        )

    room_ids = [entry.room_id for entry in page]
    rooms_data = await _fetch_room_state(
        room_store, room_ids, event_types_with_required
    )
    room_stats = await _fetch_room_stats(room_store, room_ids)

    courses: List[Course] = [
        _build_course(
            entry,
            rooms_data.get(entry.room_id, {}),
            room_stats.get(entry.room_id, {}),
        )
        for entry in page
    ]

    next_batch = page[-1].room_id if has_next else None

    return PublicCoursesResponse(
        chunk=courses,
        next_batch=next_batch,
        total_room_count_estimate=total_room_count,
    )
