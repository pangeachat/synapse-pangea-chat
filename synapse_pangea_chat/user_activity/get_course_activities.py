from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional

from synapse.storage.databases.main.room import RoomStore

logger = logging.getLogger("synapse_pangea_chat.user_activity.get_course_activities")

PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE = "pangea.activity_plan"
PANGEA_COURSE_PLAN_STATE_EVENT_TYPE = "pangea.course_plan"


async def get_course_activities(
    room_store: RoomStore,
    course_room_id: str,
    *,
    include_user_id: Optional[str] = None,
    exclude_user_id: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
) -> Dict[str, Any]:
    """Return activity rooms for a course, optionally filtered by user membership.

    Query params (mutually exclusive):
        include_user_id — only return activities where the user IS a member
        exclude_user_id — only return activities where the user is NOT a member

    Response shape:
        {
            "course_room_id": "!abc:example.com",
            "activities": [
                {
                    "room_id": "!def:example.com",
                    "room_name": "Activity 1",
                    "activity_id": "act-123",
                    "members": ["@alice:example.com"],
                    "created_ts": 1700000000000
                }
            ]
        }
    """

    # --- 1. Verify the room is a course (has pangea.course_plan state) --------
    verify_query = """
    SELECT 1 FROM current_state_events cse
    WHERE cse.room_id = ? AND cse.type = ?
    LIMIT 1
    """
    verify_rows = await room_store.db_pool.execute(
        "verify_course_room",
        verify_query,
        course_room_id,
        PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
    )
    if not verify_rows:
        return {"error": "Room is not a course", "course_room_id": course_room_id}

    # --- 2. Find child activity rooms via m.space.child on the course ---------
    #    We look for rooms that have m.space.parent pointing to this course
    #    AND have a pangea.activity_plan state event.
    children_query = """
    SELECT cse_parent.room_id
    FROM current_state_events cse_parent
    INNER JOIN current_state_events cse_activity
        ON cse_activity.room_id = cse_parent.room_id
    WHERE cse_parent.type = 'm.space.parent'
      AND cse_parent.state_key = ?
      AND cse_activity.type = ?
    """
    children_rows = await room_store.db_pool.execute(
        "get_course_activity_rooms",
        children_query,
        course_room_id,
        PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    )

    activity_room_ids = [row[0] for row in children_rows]
    if not activity_room_ids:
        return {"course_room_id": course_room_id, "activities": []}

    placeholders = ",".join(["?" for _ in activity_room_ids])

    # --- 3. Get activity_id for each room -------------------------------------
    state_query = f"""
    SELECT cse.room_id, ej.json
    FROM current_state_events cse
    INNER JOIN event_json ej ON ej.event_id = cse.event_id
    WHERE cse.room_id IN ({placeholders})
      AND cse.type = ?
    """
    state_rows = await room_store.db_pool.execute(
        "get_course_act_state",
        state_query,
        *activity_room_ids,
        PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    )

    activity_ids: Dict[str, Optional[str]] = {}
    for row in state_rows:
        room_id, json_data = row
        if isinstance(json_data, str):
            event_data = json.loads(json_data)
        else:
            event_data = json_data
        content = event_data.get("content", {}) if isinstance(event_data, dict) else {}
        activity_ids[room_id] = content.get("activity_id")

    # --- 4. Room names --------------------------------------------------------
    name_query = f"""
    SELECT cse.room_id, ej.json
    FROM current_state_events cse
    INNER JOIN event_json ej ON ej.event_id = cse.event_id
    WHERE cse.room_id IN ({placeholders})
      AND cse.type = 'm.room.name'
    """
    name_rows = await room_store.db_pool.execute(
        "get_course_act_names", name_query, *activity_room_ids
    )
    room_names: Dict[str, Optional[str]] = {}
    for row in name_rows:
        room_id, json_data = row
        if isinstance(json_data, str):
            event_data = json.loads(json_data)
        else:
            event_data = json_data
        content = event_data.get("content", {}) if isinstance(event_data, dict) else {}
        room_names[room_id] = content.get("name")

    # --- 5. Members -----------------------------------------------------------
    members_query = f"""
    SELECT rm.room_id, rm.user_id
    FROM room_memberships rm
    INNER JOIN current_state_events cse
        ON cse.event_id = rm.event_id
    WHERE rm.room_id IN ({placeholders})
      AND rm.membership = 'join'
    """
    members_rows = await room_store.db_pool.execute(
        "get_course_act_members", members_query, *activity_room_ids
    )
    activity_members: Dict[str, List[str]] = {}
    for row in members_rows:
        room_id, member_user_id = row
        if room_id not in activity_members:
            activity_members[room_id] = []
        activity_members[room_id].append(member_user_id)

    # --- 6. Creation timestamps -----------------------------------------------
    creation_query = f"""
    SELECT cse.room_id, e.origin_server_ts
    FROM current_state_events cse
    INNER JOIN events e ON e.event_id = cse.event_id
    WHERE cse.room_id IN ({placeholders})
      AND cse.type = 'm.room.create'
    """
    creation_rows = await room_store.db_pool.execute(
        "get_course_act_creation", creation_query, *activity_room_ids
    )
    creation_ts: Dict[str, int] = {}
    for row in creation_rows:
        room_id, ts = row
        creation_ts[room_id] = ts or 0

    # --- 7. Assemble & filter -------------------------------------------------
    activities: List[Dict[str, Any]] = []
    for rid in activity_room_ids:
        members = activity_members.get(rid, [])

        # Apply user membership filter
        if include_user_id and include_user_id not in members:
            continue
        if exclude_user_id and exclude_user_id in members:
            continue

        activities.append(
            {
                "room_id": rid,
                "room_name": room_names.get(rid),
                "activity_id": activity_ids.get(rid),
                "members": members,
                "created_ts": creation_ts.get(rid, 0),
            }
        )

    # Sort by created_ts descending (most recent first)
    activities.sort(key=lambda x: x["created_ts"], reverse=True)

    # --- 8. Paginate ----------------------------------------------------------
    total_docs = len(activities)
    max_page = max(1, math.ceil(total_docs / limit))
    if page < 1:
        page = 1
    if page > max_page:
        page = max_page

    offset = (page - 1) * limit
    paged_activities = activities[offset : offset + limit]

    return {
        "course_room_id": course_room_id,
        "activities": paged_activities,
        "page": page,
        "limit": limit,
        "totalDocs": total_docs,
        "maxPage": max_page,
    }
