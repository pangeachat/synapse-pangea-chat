import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from synapse.api.constants import HistoryVisibility
from synapse.storage.databases.main.room import RoomStore

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.public_courses.course_metadata_cache import (
    CourseMeta,
    FilteredCourseMetadataLookupError,
    get_course_metadata,
    get_filtered_course_ids,
)
from synapse_pangea_chat.public_courses.types import (
    Course,
    CourseFilters,
    PublicCoursesResponse,
)

# In-memory cache for course preview data
# Structure: {room_id: (data, timestamp)}
EventContent = Dict[str, Any]
StateKeyMap = Dict[Optional[str], EventContent]
RoomStateMap = Dict[str, StateKeyMap]
AllRoomsState = Dict[str, RoomStateMap]

_cache: Dict[str, Tuple[RoomStateMap, float]] = {}
_CACHE_TTL_SECONDS = 60  # 1 minute TTL

logger = logging.getLogger("synapse_pangea_chat.get_public_courses")
logger.setLevel(logging.DEBUG)

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

_FILTERING_WARNING_NONE = ""
_FILTERING_WARNING_CMS_NOT_CONFIGURED = "Language filters could not be applied: CMS is not configured; returned unfiltered results."
_FILTERING_WARNING_CMS_LOOKUP_FAILED = "Language filters could not be applied: CMS lookup failed; returned unfiltered results."


def _is_cache_valid(timestamp: float) -> bool:
    """Check if a cache entry is still valid based on TTL."""
    return time.time() - timestamp < _CACHE_TTL_SECONDS


def _get_cached_room(room_id: str) -> Optional[RoomStateMap]:
    """Get cached room data if it exists and is still valid."""
    if room_id in _cache:
        data, timestamp = _cache[room_id]
        if _is_cache_valid(timestamp):
            return data
        else:
            # Remove expired entry
            del _cache[room_id]
    return None


def _cache_room_data(room_id: str, data: RoomStateMap) -> None:
    """Cache room data with current timestamp."""
    _cache[room_id] = (data, time.time())


def _cleanup_expired_cache() -> None:
    """Remove expired entries from cache."""
    current_time = time.time()
    expired_keys = [
        room_id
        for room_id, (_, timestamp) in _cache.items()
        if current_time - timestamp >= _CACHE_TTL_SECONDS
    ]
    for room_id in expired_keys:
        del _cache[room_id]


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


async def get_public_courses(
    room_store: RoomStore,
    config: PangeaChatConfig,
    limit: int,
    since: Optional[str],
    filters: Optional[CourseFilters] = None,
) -> PublicCoursesResponse:
    logger.debug("Executing public courses query")

    has_filters = bool(filters)

    # Clean up expired cache entries periodically
    _cleanup_expired_cache()

    required_course_event_type = getattr(
        config,
        "course_plan_state_event_type",
        DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
    )
    if (
        not isinstance(required_course_event_type, str)
        or not required_course_event_type
    ):
        required_course_event_type = DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE

    # Ensure we always fetch the required course event type alongside any configured preview events
    event_types_with_required = list(
        dict.fromkeys(RESPONSE_STATE_EVENTS + (required_course_event_type,))
    )

    # Normalise limit to a sensible value
    if limit <= 0:
        limit = 10

    start_index = 0
    if since:
        try:
            start_index = int(since)
            if start_index < 0:
                start_index = 0
        except (ValueError, TypeError):
            start_index = 0

    total_count_query = """
SELECT COUNT(DISTINCT se.room_id)
FROM state_events se
INNER JOIN rooms r ON r.room_id = se.room_id
WHERE se.type = ?
  AND r.is_public = 't'
    """

    total_count_rows = await room_store.db_pool.execute(
        "get_public_courses_total_count",
        total_count_query,
        required_course_event_type,
    )

    total_room_count = 0
    if total_count_rows:
        try:
            total_room_count = int(total_count_rows[0][0])
        except (TypeError, ValueError, IndexError):
            total_room_count = 0

    # Short-circuit when there are no public courses
    if total_room_count == 0:
        return PublicCoursesResponse(
            chunk=[],
            filtering_warning=_FILTERING_WARNING_NONE,
            next_batch=None,
            prev_batch=None,
            total_room_count_estimate=0,
        )

    if has_filters:
        return await _get_filtered_public_courses(
            room_store,
            config,
            limit,
            start_index,
            total_room_count,
            required_course_event_type,
            event_types_with_required,
            filters,  # type: ignore[arg-type]
        )

    return await _get_unfiltered_public_courses(
        room_store,
        config,
        limit,
        start_index,
        total_room_count,
        required_course_event_type,
        event_types_with_required,
    )


# ---------------------------------------------------------------------------
# Shared helpers
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
SELECT DISTINCT ON (e.room_id, e.type, e.state_key)
        e.room_id, e.type, e.state_key, ej.json
FROM events e
INNER JOIN state_events se ON e.event_id = se.event_id
INNER JOIN event_json ej ON e.event_id = ej.event_id
WHERE e.room_id IN ({room_placeholders})
  AND e.type IN ({event_type_placeholders})
  AND se.type = e.type
  AND (se.state_key = e.state_key OR (se.state_key IS NULL AND e.state_key IS NULL))
ORDER BY e.room_id, e.type, e.state_key, e.origin_server_ts DESC
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
    room_id: str,
    room_data: RoomStateMap,
    required_course_event_type: str,
    stats: Dict[str, Any],
    meta: Optional[CourseMeta] = None,
) -> Optional[Course]:
    """Build a single Course dict from room state + optional CMS metadata."""
    course_event_state = room_data.get(required_course_event_type)
    if not course_event_state:
        return None

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

    course_plan_content = _get_event_content(course_event_state)
    course_id = course_plan_content.get("uuid")

    history_visibility = stats.get("history_visibility")
    guest_access = stats.get("guest_access")
    join_rules = stats.get("join_rules")
    room_type = stats.get("room_type")
    joined_members = stats.get("joined_members", 0)

    course: Course = {
        "room_id": room_id,
        "name": name,
        "topic": topic,
        "avatar_url": avatar_url,
        "canonical_alias": canonical_alias,
        "course_id": course_id,
        "num_joined_members": joined_members,
        "world_readable": history_visibility == HistoryVisibility.WORLD_READABLE,
        "guest_can_join": guest_access == "can_join",
        "join_rule": join_rules,
        "room_type": room_type,
        "target_language": meta["l2"] if meta else None,
        "language_of_instructions": meta["l1"] if meta else None,
        "cefr_level": meta["cefr_level"] if meta else None,
    }
    return course


def _extract_course_uuid(
    room_data: RoomStateMap,
    required_course_event_type: str,
) -> Optional[str]:
    """Extract the CMS UUID from a room's course plan state event."""
    event_state = room_data.get(required_course_event_type)
    if not event_state:
        return None
    return _get_event_content(event_state).get("uuid")


def _with_filtering_warning(
    response: PublicCoursesResponse,
    warning: str,
) -> PublicCoursesResponse:
    response["filtering_warning"] = warning
    return response


# ---------------------------------------------------------------------------
# Unfiltered path (existing behaviour + language metadata enrichment)
# ---------------------------------------------------------------------------


async def _get_unfiltered_public_courses(
    room_store: RoomStore,
    config: PangeaChatConfig,
    limit: int,
    start_index: int,
    total_room_count: int,
    required_course_event_type: str,
    event_types_with_required: List[str],
) -> PublicCoursesResponse:
    overfetch_limit = limit + 1

    room_ids_query = """
SELECT room_id FROM (
    SELECT DISTINCT se.room_id AS room_id
    FROM state_events se
    INNER JOIN rooms r ON r.room_id = se.room_id
    WHERE se.type = ?
      AND r.is_public = 't'
) room_ids
ORDER BY room_id
OFFSET ?
LIMIT ?
    """

    room_rows = await room_store.db_pool.execute(
        "get_public_courses_room_ids",
        room_ids_query,
        required_course_event_type,
        start_index,
        overfetch_limit,
    )

    if not room_rows:
        prev_batch = None if start_index <= 0 else str(max(0, start_index - limit))
        return PublicCoursesResponse(
            chunk=[],
            filtering_warning=_FILTERING_WARNING_NONE,
            next_batch=None,
            prev_batch=prev_batch,
            total_room_count_estimate=total_room_count,
        )

    room_ids = [row[0] for row in room_rows]
    has_next = len(room_ids) > limit
    display_room_ids = room_ids[:limit]

    rooms_data = await _fetch_room_state(
        room_store, display_room_ids, event_types_with_required
    )
    room_stats = await _fetch_room_stats(room_store, display_room_ids)

    # Collect UUIDs for CMS metadata enrichment
    uuid_to_room: Dict[str, str] = {}
    for room_id in display_room_ids:
        rd = rooms_data.get(room_id)
        if rd:
            uuid = _extract_course_uuid(rd, required_course_event_type)
            if uuid:
                uuid_to_room[uuid] = room_id

    # Fetch language metadata from CMS (best-effort)
    cms_meta: Dict[str, CourseMeta] = {}
    if uuid_to_room and config.cms_base_url and config.cms_service_api_key:
        cms_meta = await get_course_metadata(
            list(uuid_to_room.keys()),
            config.cms_base_url,
            config.cms_service_api_key,
            cache_ttl=config.public_courses_cms_cache_ttl_seconds,
        )

    courses: List[Course] = []
    for room_id in display_room_ids:
        rd = rooms_data.get(room_id)
        if not rd:
            continue
        uuid = _extract_course_uuid(rd, required_course_event_type)
        meta = cms_meta.get(uuid) if uuid else None
        course = _build_course(
            room_id,
            rd,
            required_course_event_type,
            room_stats.get(room_id, {}),
            meta=meta,
        )
        if course:
            courses.append(course)

    next_batch = None
    if has_next and (start_index + limit) < total_room_count:
        next_batch = str(start_index + limit)

    prev_batch = None if start_index <= 0 else str(max(0, start_index - limit))

    return PublicCoursesResponse(
        chunk=courses,
        filtering_warning=_FILTERING_WARNING_NONE,
        next_batch=next_batch,
        prev_batch=prev_batch,
        total_room_count_estimate=total_room_count,
    )


# ---------------------------------------------------------------------------
# Filtered path (CMS-side filtering, Python-side pagination)
# ---------------------------------------------------------------------------


async def _get_filtered_public_courses(
    room_store: RoomStore,
    config: PangeaChatConfig,
    limit: int,
    start_index: int,
    total_room_count: int,
    required_course_event_type: str,
    event_types_with_required: List[str],
    filters: CourseFilters,
) -> PublicCoursesResponse:
    # 1. Fetch ALL public course room IDs (no SQL pagination)
    all_rooms_query = """
SELECT DISTINCT se.room_id
FROM state_events se
INNER JOIN rooms r ON r.room_id = se.room_id
WHERE se.type = ?
  AND r.is_public = 't'
ORDER BY se.room_id
    """

    all_room_rows = await room_store.db_pool.execute(
        "get_public_courses_all_room_ids",
        all_rooms_query,
        required_course_event_type,
    )

    if not all_room_rows:
        return PublicCoursesResponse(
            chunk=[],
            filtering_warning=_FILTERING_WARNING_NONE,
            next_batch=None,
            prev_batch=None,
            total_room_count_estimate=0,
        )

    all_room_ids = [row[0] for row in all_room_rows]

    # 2. Fetch state events for ALL rooms to extract UUIDs
    rooms_data = await _fetch_room_state(
        room_store, all_room_ids, event_types_with_required
    )

    uuid_to_room: Dict[str, str] = {}
    for room_id in all_room_ids:
        rd = rooms_data.get(room_id)
        if rd:
            uuid = _extract_course_uuid(rd, required_course_event_type)
            if uuid:
                uuid_to_room[uuid] = room_id

    if not uuid_to_room:
        return PublicCoursesResponse(
            chunk=[],
            filtering_warning=_FILTERING_WARNING_NONE,
            next_batch=None,
            prev_batch=None,
            total_room_count_estimate=0,
        )

    # 3. Ask CMS which UUIDs match the filters
    if not config.cms_base_url or not config.cms_service_api_key:
        logger.warning(
            "CMS not configured for language filters; falling back to unfiltered"
        )
        return _with_filtering_warning(
            await _get_unfiltered_public_courses(
                room_store,
                config,
                limit,
                start_index,
                total_room_count,
                required_course_event_type,
                event_types_with_required,
            ),
            _FILTERING_WARNING_CMS_NOT_CONFIGURED,
        )

    try:
        matched_meta = await get_filtered_course_ids(
            list(uuid_to_room.keys()),
            config.cms_base_url,
            config.cms_service_api_key,
            target_language=filters.get("target_language"),
            language_of_instructions=filters.get("language_of_instructions"),
            cefr_level=filters.get("cefr_level"),
        )
    except FilteredCourseMetadataLookupError:
        logger.warning("Language-filter CMS lookup failed; falling back to unfiltered")
        return _with_filtering_warning(
            await _get_unfiltered_public_courses(
                room_store,
                config,
                limit,
                start_index,
                total_room_count,
                required_course_event_type,
                event_types_with_required,
            ),
            _FILTERING_WARNING_CMS_LOOKUP_FAILED,
        )

    # Preserve deterministic ordering (same as SQL ORDER BY room_id)
    matched_room_ids = [
        uuid_to_room[uuid] for uuid in uuid_to_room if uuid in matched_meta
    ]
    # Re-sort by room_id for stable pagination
    matched_room_ids.sort()

    filtered_total = len(matched_room_ids)

    # 4. Python-side pagination over the filtered set
    page = matched_room_ids[start_index : start_index + limit]

    if not page:
        prev_batch = None if start_index <= 0 else str(max(0, start_index - limit))
        return PublicCoursesResponse(
            chunk=[],
            filtering_warning=_FILTERING_WARNING_NONE,
            next_batch=None,
            prev_batch=prev_batch,
            total_room_count_estimate=filtered_total,
        )

    room_stats = await _fetch_room_stats(room_store, page)

    courses: List[Course] = []
    for room_id in page:
        rd = rooms_data.get(room_id)
        if not rd:
            continue
        uuid = _extract_course_uuid(rd, required_course_event_type)
        meta = matched_meta.get(uuid) if uuid else None
        course = _build_course(
            room_id,
            rd,
            required_course_event_type,
            room_stats.get(room_id, {}),
            meta=meta,
        )
        if course:
            courses.append(course)

    has_next = (start_index + limit) < filtered_total
    next_batch = str(start_index + limit) if has_next else None
    prev_batch = None if start_index <= 0 else str(max(0, start_index - limit))

    return PublicCoursesResponse(
        chunk=courses,
        filtering_warning=_FILTERING_WARNING_NONE,
        next_batch=next_batch,
        prev_batch=prev_batch,
        total_room_count_estimate=filtered_total,
    )
