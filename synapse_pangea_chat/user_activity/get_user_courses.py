from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from synapse.storage.databases.main.room import RoomStore

logger = logging.getLogger("synapse_pangea_chat.user_activity.get_user_courses")

PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE = "pangea.activity_plan"
PANGEA_COURSE_PLAN_STATE_EVENT_TYPE = "pangea.course_plan"


async def get_user_courses(
    room_store: RoomStore,
    user_id: str,
    *,
    page: int = 1,
    limit: int = 50,
) -> Dict[str, Any]:
    """Return paginated list of courses/activity rooms a user is a member of.

    Response shape:
        {
            "user_id": "@alice:example.com",
            "docs": [
                {
                    "room_id": "!abc:example.com",
                    "room_name": "Spanish 101",
                    "is_course": true,
                    "is_activity": false,
                    "activity_id": null,
                    "parent_course_room_id": null,
                    "most_recent_activity_ts": 1700000000000
                }
            ],
            "page": 1,
            "limit": 50,
            "totalDocs": 12,
            "maxPage": 1
        }
    """

    # --- 1. Find all course/activity rooms the user is a member of -----------
    memberships_query = """
    SELECT DISTINCT rm.room_id
    FROM room_memberships rm
    INNER JOIN current_state_events cse_check
        ON cse_check.room_id = rm.room_id
    WHERE rm.user_id = ?
      AND rm.membership = 'join'
      AND cse_check.type IN (?, ?)
    """
    membership_rows = await room_store.db_pool.execute(
        "get_user_courses_memberships",
        memberships_query,
        user_id,
        PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
        PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    )

    all_room_ids = [row[0] for row in membership_rows]
    total_docs = len(all_room_ids)
    max_page = max(1, math.ceil(total_docs / limit))
    if page < 1:
        page = 1
    if page > max_page:
        page = max_page

    if not all_room_ids:
        return {
            "user_id": user_id,
            "docs": [],
            "page": page,
            "limit": limit,
            "totalDocs": 0,
            "maxPage": 1,
        }

    # --- 2. State events for room classification -----------------------------
    room_placeholders = ",".join(["?" for _ in all_room_ids])
    state_events_query = f"""
    SELECT cse.room_id, cse.type, cse.state_key, ej.json
    FROM current_state_events cse
    INNER JOIN event_json ej ON ej.event_id = cse.event_id
    WHERE cse.room_id IN ({room_placeholders})
      AND cse.type IN (?, ?)
    """
    state_params: Tuple[Any, ...] = (
        *all_room_ids,
        PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
        PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    )
    state_rows = await room_store.db_pool.execute(
        "get_user_courses_state_events", state_events_query, *state_params
    )

    room_info: Dict[str, Dict[str, Any]] = {}
    for row in state_rows:
        room_id, event_type, _, json_data = row
        if room_id not in room_info:
            room_info[room_id] = {
                "room_id": room_id,
                "is_course": False,
                "is_activity": False,
                "activity_id": None,
            }
        if isinstance(json_data, str):
            event_data = json.loads(json_data)
        else:
            event_data = json_data
        content = event_data.get("content", {}) if isinstance(event_data, dict) else {}
        if event_type == PANGEA_COURSE_PLAN_STATE_EVENT_TYPE:
            room_info[room_id]["is_course"] = True
        elif event_type == PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE:
            room_info[room_id]["is_activity"] = True
            room_info[room_id]["activity_id"] = content.get("activity_id")

    # --- 3. Room names -------------------------------------------------------
    name_query = f"""
    SELECT cse.room_id, ej.json
    FROM current_state_events cse
    INNER JOIN event_json ej ON ej.event_id = cse.event_id
    WHERE cse.room_id IN ({room_placeholders})
      AND cse.type = 'm.room.name'
    """
    name_rows = await room_store.db_pool.execute(
        "get_user_courses_room_names", name_query, *all_room_ids
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

    # --- 4. Activity → parent course via m.space.parent ----------------------
    activity_room_ids = [
        rid for rid, info in room_info.items() if info.get("is_activity")
    ]
    activity_to_course: Dict[str, Optional[str]] = {}
    if activity_room_ids:
        act_placeholders = ",".join(["?" for _ in activity_room_ids])
        parent_query = f"""
        SELECT cse.room_id, cse.state_key
        FROM current_state_events cse
        WHERE cse.room_id IN ({act_placeholders})
          AND cse.type = 'm.space.parent'
        """
        parent_rows = await room_store.db_pool.execute(
            "get_user_courses_parents", parent_query, *activity_room_ids
        )
        for row in parent_rows:
            activity_room_id, parent_room_id = row
            if parent_room_id in room_info and room_info[parent_room_id].get(
                "is_course"
            ):
                activity_to_course[activity_room_id] = parent_room_id

    # --- 5. Most recent activity per course + activity rooms ------------------
    # Get the user's last message in each room (for sorting by recency)
    last_msg_query = f"""
    SELECT e.room_id, MAX(e.origin_server_ts) AS last_ts
    FROM events e
    WHERE e.type = 'm.room.message'
      AND e.sender = ?
      AND e.room_id IN ({room_placeholders})
    GROUP BY e.room_id
    """
    last_msg_rows = await room_store.db_pool.execute(
        "get_user_courses_last_msg", last_msg_query, user_id, *all_room_ids
    )
    room_last_msg: Dict[str, int] = {}
    for row in last_msg_rows:
        room_id, last_ts = row
        room_last_msg[room_id] = last_ts or 0

    # For courses: aggregate activity from both the course room and child
    # activity rooms to get the most_recent_activity_ts
    course_room_ids = [
        rid for rid, info in room_info.items() if info.get("is_course")
    ]
    course_activity_ts: Dict[str, int] = {}
    for crid in course_room_ids:
        course_activity_ts[crid] = room_last_msg.get(crid, 0)

    # Activity room timestamps bubble up to their parent course
    for arid, parent_crid in activity_to_course.items():
        if parent_crid:
            current = course_activity_ts.get(parent_crid, 0)
            act_ts = room_last_msg.get(arid, 0)
            course_activity_ts[parent_crid] = max(current, act_ts)

    # --- 6. Assemble course entries ------------------------------------------
    courses: List[Dict[str, Any]] = []
    for rid in all_room_ids:
        info = room_info.get(rid)
        if not info:
            continue

        # For activity rooms, most_recent_activity_ts is just their own last_msg
        # For course rooms, it's the aggregated value including child activities
        if info.get("is_course"):
            most_recent_ts = course_activity_ts.get(rid, 0)
        else:
            most_recent_ts = room_last_msg.get(rid, 0)

        courses.append(
            {
                "room_id": rid,
                "room_name": room_names.get(rid),
                "is_course": info.get("is_course", False),
                "is_activity": info.get("is_activity", False),
                "activity_id": info.get("activity_id"),
                "parent_course_room_id": activity_to_course.get(rid),
                "most_recent_activity_ts": most_recent_ts,
            }
        )

    # Sort by most_recent_activity_ts descending (most active first)
    courses.sort(key=lambda x: x["most_recent_activity_ts"], reverse=True)

    # --- 7. Apply pagination --------------------------------------------------
    offset = (page - 1) * limit
    paged_courses = courses[offset : offset + limit]

    return {
        "user_id": user_id,
        "docs": paged_courses,
        "page": page,
        "limit": limit,
        "totalDocs": total_docs,
        "maxPage": max_page,
    }
