from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from synapse.storage.databases.main.room import RoomStore

logger = logging.getLogger(
    "synapse_pangea_chat.user_activity.get_course_activity_summary"
)

PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE = "pangea.activity_plan"
PANGEA_COURSE_PLAN_STATE_EVENT_TYPE = "pangea.course_plan"


async def get_course_activity_summary(
    room_store: RoomStore,
    course_room_id: str,
) -> Dict[str, Any]:
    """Return lightweight per-activity metadata for a course.

    Unlike get_course_activities, this returns only the fields needed for
    topic unlock logic: activity_id, member_count, and number_of_participants.
    No pagination — course activity counts are bounded (typically <50).
    """

    # --- 1. Verify the room is a course (has pangea.course_plan state) --------
    verify_query = """
    SELECT 1 FROM current_state_events cse
    WHERE cse.room_id = ? AND cse.type = ?
    LIMIT 1
    """
    verify_rows = await room_store.db_pool.execute(
        "verify_course_room_summary",
        verify_query,
        course_room_id,
        PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
    )
    if not verify_rows:
        return {"error": "Room is not a course", "course_room_id": course_room_id}

    # --- 2. Find child activity rooms via m.space.parent + activity_plan ------
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
        "get_course_activity_rooms_summary",
        children_query,
        course_room_id,
        PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    )

    activity_room_ids = [row[0] for row in children_rows]
    if not activity_room_ids:
        return {"course_room_id": course_room_id, "activities": []}

    placeholders = ",".join(["?" for _ in activity_room_ids])

    # --- 3. Get activity_id + number_of_participants from state event ---------
    state_query = f"""
    SELECT cse.room_id, ej.json
    FROM current_state_events cse
    INNER JOIN event_json ej ON ej.event_id = cse.event_id
    WHERE cse.room_id IN ({placeholders})
      AND cse.type = ?
    """
    state_rows = await room_store.db_pool.execute(
        "get_course_act_state_summary",
        state_query,
        *activity_room_ids,
        PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    )

    activity_meta: Dict[str, Dict[str, Any]] = {}
    for row in state_rows:
        room_id, json_data = row
        if isinstance(json_data, str):
            event_data = json.loads(json_data)
        else:
            event_data = json_data
        content = event_data.get("content", {}) if isinstance(event_data, dict) else {}
        activity_id: Optional[str] = content.get("activity_id")
        req = content.get("req", {})
        number_of_participants: Optional[int] = (
            req.get("number_of_participants") if isinstance(req, dict) else None
        )
        activity_meta[room_id] = {
            "activity_id": activity_id,
            "number_of_participants": number_of_participants,
        }

    # --- 4. Member counts (COUNT instead of full member list) -----------------
    count_query = f"""
    SELECT rm.room_id, COUNT(*) AS member_count
    FROM room_memberships rm
    INNER JOIN current_state_events cse
        ON cse.event_id = rm.event_id
    WHERE rm.room_id IN ({placeholders})
      AND rm.membership = 'join'
    GROUP BY rm.room_id
    """
    count_rows = await room_store.db_pool.execute(
        "get_course_act_member_counts", count_query, *activity_room_ids
    )
    member_counts: Dict[str, int] = {}
    for row in count_rows:
        room_id, count = row
        member_counts[room_id] = count

    # --- 5. Assemble ----------------------------------------------------------
    activities: List[Dict[str, Any]] = []
    for rid in activity_room_ids:
        meta = activity_meta.get(rid, {})
        activities.append(
            {
                "room_id": rid,
                "activity_id": meta.get("activity_id"),
                "member_count": member_counts.get(rid, 0),
                "number_of_participants": meta.get("number_of_participants"),
            }
        )

    return {
        "course_room_id": course_room_id,
        "activities": activities,
    }
