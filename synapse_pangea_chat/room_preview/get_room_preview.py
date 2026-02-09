import json
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from synapse.module_api import ModuleApi
from synapse.storage.databases.main.room import RoomStore

from synapse_pangea_chat.room_preview.constants import (
    EVENT_TYPE_M_ROOM_JOIN_RULES,
    EVENT_TYPE_M_ROOM_MEMBER,
    JOIN_RULE_CONTENT_KEY,
    MEMBERSHIP_CONTENT_KEY,
    PANGEA_ACTIVITY_ROLE_STATE_EVENT_TYPE,
    PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
)

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_preview.get_room_preview"
)

# In-memory cache for room preview data
# Structure: {room_id: (data, timestamp)}
_room_cache: Dict[str, Tuple[Dict[str, Dict[str, Any]], float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes TTL (increased due to reactive invalidation)


def _is_cache_valid(timestamp: float) -> bool:
    """Check if a cache entry is still valid based on TTL."""
    return time.time() - timestamp < _CACHE_TTL_SECONDS


def _get_cached_room(room_id: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """Get cached room data if it exists and is still valid."""
    if room_id in _room_cache:
        data, timestamp = _room_cache[room_id]
        if _is_cache_valid(timestamp):
            return data
        else:
            # Remove expired entry
            del _room_cache[room_id]
    return None


def _cache_room_data(room_id: str, data: Dict[str, Dict[str, Any]]) -> None:
    """Cache room data with current timestamp."""
    _room_cache[room_id] = (data, time.time())


def _cleanup_expired_cache() -> None:
    """Remove expired entries from cache."""
    current_time = time.time()
    expired_keys = [
        room_id
        for room_id, (_, timestamp) in _room_cache.items()
        if current_time - timestamp >= _CACHE_TTL_SECONDS
    ]
    for room_id in expired_keys:
        del _room_cache[room_id]


def invalidate_room_cache(room_id: str) -> None:
    """
    Invalidate cached data for a specific room.

    This function is called reactively when state events change in a room.

    :param room_id: The room ID to invalidate from cache
    """
    _room_cache.pop(room_id, None)


def _filter_join_rules_content(event_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Filter m.room.join_rules event content to only include the 'join_rule' key.

    This ensures we don't expose sensitive or unnecessary information from the
    join_rules state event. Only the 'join_rule' key is included in the content.

    :param event_data: The full Matrix event data containing 'content' field
    :return: A copy of event_data with filtered content
    """
    if not isinstance(event_data, dict):
        return event_data

    content = event_data.get("content", {})
    if not isinstance(content, dict):
        return event_data

    # Only keep the join_rule key in content
    filtered_content = {}
    if JOIN_RULE_CONTENT_KEY in content:
        filtered_content[JOIN_RULE_CONTENT_KEY] = content[JOIN_RULE_CONTENT_KEY]

    # Create a copy of the event data with filtered content
    filtered_event = event_data.copy()
    filtered_event["content"] = filtered_content

    return filtered_event


async def _get_membership_summary(
    room_id: str, api: ModuleApi, room_store: RoomStore
) -> Dict[str, str]:
    """
    Get a summary of user memberships in the room.

    :param room_id: The room ID to get membership summary for
    :param api: The ModuleApi instance to query room state
    :param room_store: The RoomStore instance (kept for compatibility, not used)
    :return: Dictionary mapping user_id to membership status (e.g., "join", "leave")
    """
    membership_summary: Dict[str, str] = {}

    try:
        # Get all membership events for the room
        membership_events = await api.get_room_state(room_id)

        # Build membership summary
        for (event_type, state_key), event in membership_events.items():
            if event_type == EVENT_TYPE_M_ROOM_MEMBER:
                if hasattr(event, "content") and event.content:
                    membership = event.content.get(MEMBERSHIP_CONTENT_KEY)
                    if membership:
                        membership_summary[state_key] = membership
                elif isinstance(event, dict):
                    # Handle case where event is a dict
                    content = event.get("content", {})
                    membership = content.get(MEMBERSHIP_CONTENT_KEY)
                    if membership:
                        membership_summary[state_key] = membership

    except Exception as e:
        # Log the error but still return empty dict to ensure robustness
        logger.error(
            "Failed to get membership summary for room %s: %s",
            room_id,
            e,
            exc_info=True,
        )

    return membership_summary


def _add_membership_summary(
    room_data: Dict[str, Dict[str, Any]], membership_summary: Dict[str, str]
) -> None:
    """
    Add membership summary to room data for activity rooms or course rooms.

    For activity rooms (rooms with pangea.activity_roles):
    - The membership summary only includes users who are referenced in the activity
      roles state event. This allows clients to determine who has left the room
      while still seeing all roles (including those of users who have left).

    For course rooms (rooms with pangea.course_plan but no activity_roles):
    - The membership summary includes all users in the room. This allows clients
      to see the current membership state of the course room.

    :param room_data: The room data dictionary to modify in-place
    :param membership_summary: Dictionary mapping user_id to membership status
    """
    has_activity_roles = PANGEA_ACTIVITY_ROLE_STATE_EVENT_TYPE in room_data
    has_course_plan = PANGEA_COURSE_PLAN_STATE_EVENT_TYPE in room_data

    # Only add membership_summary for activity rooms or course rooms
    if not has_activity_roles and not has_course_plan:
        return

    if has_activity_roles:
        # For activity rooms, filter to only users in activity roles
        activity_roles = room_data[PANGEA_ACTIVITY_ROLE_STATE_EVENT_TYPE]

        # Collect all user IDs from the activity roles
        user_ids_in_roles: set[str] = set()
        for event_data in activity_roles.values():
            if not isinstance(event_data, dict):
                continue

            content = event_data.get("content", {})
            if not isinstance(content, dict):
                continue

            roles = content.get("roles", {})
            if not isinstance(roles, dict):
                continue

            for role_data in roles.values():
                if isinstance(role_data, dict):
                    user_id = role_data.get("user_id")
                    if user_id:
                        user_ids_in_roles.add(user_id)

        # Create filtered membership summary with only users in activity roles
        filtered_summary = {
            user_id: membership
            for user_id, membership in membership_summary.items()
            if user_id in user_ids_in_roles
        }

        # Add the membership summary to room_data at the top level
        room_data["membership_summary"] = filtered_summary
    else:
        # For course rooms (without activity roles), include all memberships
        room_data["membership_summary"] = membership_summary


async def get_room_preview(
    rooms: List[str],
    api: ModuleApi,
    room_store: RoomStore,
    config: "PangeaChatConfig",
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Get room preview data including state events for the specified rooms.

    Uses an in-memory cache with 5-minute TTL for individual room data to improve
    performance on repeated requests. The cache is reactively invalidated when
    relevant state events change.

    Returns a dictionary with the structure:
    {
        [room_id]: {
            [state_event_type]: {
                [state_key]: JSON
            },
            "membership_summary": {
                [user_id]: [membership_status]
            }
        }
    }

    Note: Empty matrix state key will be represented as "default" in the response.
    Note: The membership_summary only includes users who are referenced in the
          activity roles state event, allowing clients to determine who has left
          the room while still seeing all roles.

    :param rooms: List of room IDs to get preview data for.
    :param room_store: The RoomStore instance to query the database.
    :param config: The configuration containing state event types to query.
    :return: A dictionary mapping room_id to state event data organized by
             event type and state key, plus a membership_summary for activity roles.
    """
    if not rooms or not config.room_preview_state_event_types:
        return {}

    # Clean up expired cache entries periodically
    _cleanup_expired_cache()

    # Check cache for each room and separate cached vs uncached rooms
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    rooms_to_fetch: List[str] = []

    for room_id in rooms:
        cached_data = _get_cached_room(room_id)
        if cached_data is not None:
            # Get current membership summary and add to cached data
            # in case membership has changed since caching
            membership_summary = await _get_membership_summary(room_id, api, room_store)
            _add_membership_summary(cached_data, membership_summary)
            result[room_id] = cached_data
        else:
            rooms_to_fetch.append(room_id)

    # If all rooms were cached, return early
    if not rooms_to_fetch:
        return result

    # Fetch uncached rooms from database
    # Check which database backend we are using
    database_engine = room_store.db_pool.engine.module.__name__

    # Create placeholders for room IDs and event types
    room_placeholders = ",".join(
        ["?" if "sqlite" in database_engine else "%s"] * len(rooms_to_fetch)
    )
    event_type_placeholders = ",".join(
        ["?" if "sqlite" in database_engine else "%s"]
        * len(config.room_preview_state_event_types)
    )

    if "sqlite" in database_engine:
        # SQLite query
        query = f"""
            SELECT e.room_id, e.type, e.state_key, ej.json
            FROM events e
                JOIN state_events se ON e.event_id = se.event_id
                JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                e.room_id IN ({room_placeholders})
                AND e.type IN ({event_type_placeholders})
                AND se.room_id = e.room_id
                AND se.type = e.type
                AND (se.state_key = e.state_key OR (se.state_key IS NULL AND e.state_key IS NULL))
            ORDER BY e.room_id, e.type, e.state_key, e.origin_server_ts DESC
        """
        params = tuple(rooms_to_fetch + config.room_preview_state_event_types)

    else:
        # PostgreSQL query
        query = f"""
            SELECT DISTINCT ON (e.room_id, e.type, e.state_key)
                   e.room_id, e.type, e.state_key, ej.json
            FROM events e
            JOIN state_events se ON e.event_id = se.event_id
            JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                e.room_id IN ({room_placeholders})
                AND e.type IN ({event_type_placeholders})
                AND se.type = e.type
                AND (se.state_key = e.state_key OR (se.state_key IS NULL AND e.state_key IS NULL))
            ORDER BY e.room_id, e.type, e.state_key, e.origin_server_ts DESC
        """
        params = tuple(rooms_to_fetch + config.room_preview_state_event_types)

    rows = await room_store.db_pool.execute(
        "get_room_preview_state_events",
        query,
        *params,
    )

    # Initialize empty data for all rooms we're fetching
    fetched_room_data: Dict[str, Dict[str, Dict[str, Any]]] = {
        room_id: {} for room_id in rooms_to_fetch
    }

    # Process database results
    for row in rows:
        room_id, event_type, state_key, json_data = row
        if room_id not in fetched_room_data:
            fetched_room_data[room_id] = {}

        # Parse the JSON data if it's a string
        if isinstance(json_data, str):
            event_data = json.loads(json_data)
        else:
            event_data = json_data

        # Return the full Matrix event data (which contains "content" field)
        # Matrix events have a structure like: {"content": {...}, "type": "...", "state_key": "...", ...}
        # We return the complete event data

        # Store the event data, using state_key as a sub-key if present
        if event_type not in fetched_room_data[room_id]:
            fetched_room_data[room_id][event_type] = {}

        # Convert None or empty string state keys to "default"
        key = state_key if state_key is not None and state_key != "" else "default"

        # Filter m.room.join_rules content to only include join_rule key
        if event_type == EVENT_TYPE_M_ROOM_JOIN_RULES:
            event_data = _filter_join_rules_content(event_data)

        fetched_room_data[room_id][event_type][key] = event_data

    # Cache each room's data individually and add to result
    for room_id, room_data in fetched_room_data.items():
        # Get current membership summary and add to room data
        membership_summary = await _get_membership_summary(room_id, api, room_store)

        # Add membership summary for users in activity roles
        _add_membership_summary(room_data, membership_summary)

        _cache_room_data(room_id, room_data)
        result[room_id] = room_data

    return result
