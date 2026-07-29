import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from synapse.module_api import ModuleApi
from synapse.storage.databases.main.room import RoomStore

from synapse_pangea_chat.room_preview.constants import (
    MEMBERSHIP_JOIN,
    PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
)
from synapse_pangea_chat.room_preview.get_room_preview import get_room_preview

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.activity_session_previews"
)

# https://spec.matrix.org/v1.11/client-server-api/#mspacechild
EVENT_TYPE_M_SPACE_CHILD = "m.space.child"


def _placeholders(room_store: RoomStore, count: int) -> str:
    """Build a parameter-placeholder list matching the active database engine
    (sqlite uses ?, postgres uses %s) — same detection as get_room_preview."""
    database_engine = room_store.db_pool.engine.module.__name__
    return ",".join(["?" if "sqlite" in database_engine else "%s"] * count)


def _has_valid_via(content: Any) -> bool:
    """A live m.space.child link carries a non-empty list of server names in
    content.via; a removed child's event has empty content (or an invalid via)
    and must not resurface — mirrors synapse's own hierarchy filtering."""
    if not isinstance(content, dict):
        return False
    via = content.get("via")
    if not via or not isinstance(via, list):
        return False
    return all(isinstance(v, str) for v in via)


async def _member_space_ids(
    space_ids: List[str],
    requester_id: str,
    room_store: RoomStore,
) -> List[str]:
    """The subset of space_ids the requester is currently joined to. Non-member
    spaces are dropped silently so one stale space id never sinks a batch."""
    member_ids: List[str] = []
    for space_id in space_ids:
        try:
            (
                membership,
                _,
            ) = await room_store.get_local_current_membership_for_user_in_room(
                requester_id, space_id
            )
        except Exception as e:
            logger.warning(
                "Membership lookup failed for %s in %s: %s", requester_id, space_id, e
            )
            continue
        if membership == MEMBERSHIP_JOIN:
            member_ids.append(space_id)
    return member_ids


async def _child_room_ids(
    space_ids: List[str],
    room_store: RoomStore,
) -> List[str]:
    """Direct m.space.child room ids across space_ids, excluding removed
    children (no valid via) and the spaces themselves."""
    if not space_ids:
        return []

    query = f"""
        SELECT cse.room_id, cse.state_key, ej.json
        FROM current_state_events cse
        JOIN event_json ej ON cse.event_id = ej.event_id
        WHERE
            cse.room_id IN ({_placeholders(room_store, len(space_ids))})
            AND cse.type = {_placeholders(room_store, 1)}
        ORDER BY cse.room_id, cse.state_key
    """
    rows = await room_store.db_pool.execute(
        "get_activity_session_preview_space_children",
        query,
        *space_ids,
        EVENT_TYPE_M_SPACE_CHILD,
    )

    child_ids: List[str] = []
    seen: set = set(space_ids)
    for _, state_key, json_data in rows:
        if not state_key or state_key in seen:
            continue
        event_data = json.loads(json_data) if isinstance(json_data, str) else json_data
        content = event_data.get("content") if isinstance(event_data, dict) else None
        if not _has_valid_via(content):
            continue
        seen.add(state_key)
        child_ids.append(state_key)
    return child_ids


async def _activity_session_ids(
    child_room_ids: List[str],
    activity_id: Optional[str],
    room_store: RoomStore,
) -> List[str]:
    """The child rooms that are activity sessions — rooms carrying a
    pangea.activity_plan state event — optionally narrowed to one activity.

    Queries the plan event type directly, independent of the config-driven
    room_preview_state_event_types list, so a config-trimmed deploy can't
    silently break session discovery.
    """
    if not child_room_ids:
        return []

    query = f"""
        SELECT cse.room_id, ej.json
        FROM current_state_events cse
        JOIN event_json ej ON cse.event_id = ej.event_id
        WHERE
            cse.room_id IN ({_placeholders(room_store, len(child_room_ids))})
            AND cse.type = {_placeholders(room_store, 1)}
        ORDER BY cse.room_id
    """
    rows = await room_store.db_pool.execute(
        "get_activity_session_preview_plans",
        query,
        *child_room_ids,
        PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    )

    session_ids: List[str] = []
    seen: set = set()
    for room_id, json_data in rows:
        if room_id in seen:
            continue
        if activity_id is not None:
            event_data = (
                json.loads(json_data) if isinstance(json_data, str) else json_data
            )
            content = (
                event_data.get("content", {}) if isinstance(event_data, dict) else {}
            )
            if not isinstance(content, dict):
                continue
            if content.get("activity_id") != activity_id:
                continue
        seen.add(room_id)
        session_ids.append(room_id)
    return session_ids


async def get_activity_session_previews(
    space_ids: List[str],
    activity_id: Optional[str],
    requester_id: str,
    api: ModuleApi,
    room_store: RoomStore,
    config: "PangeaChatConfig",
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Preview data for the activity-session rooms under the requester's joined
    course spaces, in the same shape as get_room_preview (room_id ->
    state_event_type -> state_key -> event JSON, plus membership_summary).

    A thin front on the room_preview reader: resolve each space's direct
    children, gate on the requester's space membership, keep the children
    carrying a pangea.activity_plan (optionally one activity's), then hand the
    survivors to get_room_preview — inheriting its projection, caching, and
    invalidation. See activity-session-previews.instructions.md.

    :param space_ids: Course-space room ids the caller asks about.
    :param activity_id: Optional activity id to narrow the sessions to.
    :param requester_id: The authenticated caller; non-member spaces drop.
    :param api: ModuleApi, passed through to get_room_preview.
    :param room_store: Main datastore for the child/plan/membership queries.
    :param config: Module config (preview state event types, rate limits).
    """
    member_ids = await _member_space_ids(space_ids, requester_id, room_store)
    child_ids = await _child_room_ids(member_ids, room_store)
    session_ids = await _activity_session_ids(child_ids, activity_id, room_store)
    if not session_ids:
        return {}
    return await get_room_preview(session_ids, api, room_store, config)
