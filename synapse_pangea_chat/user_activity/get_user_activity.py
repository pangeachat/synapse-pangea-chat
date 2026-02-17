from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from synapse.storage.databases.main.room import RoomStore

logger = logging.getLogger("synapse_pangea_chat.user_activity.get_user_activity")

PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE = "pangea.activity_plan"
PANGEA_COURSE_PLAN_STATE_EVENT_TYPE = "pangea.course_plan"


async def get_user_activity(room_store: RoomStore) -> List[Dict[str, Any]]:
    """Query Synapse DB for user activity data.

    Returns per user: user_id, display_name, last_message_ts, last_login_ts,
    and their course/room memberships including activity plan info.
    """

    # 1. Get all local users with display name and last login
    users_query = """
    SELECT
        u.name AS user_id,
        p.displayname AS display_name,
        COALESCE(
            (SELECT MAX(ulip.last_seen) FROM user_ips ulip WHERE ulip.user_id = u.name),
            0
        ) AS last_login_ts
    FROM users u
    LEFT JOIN profiles p ON p.full_user_id = u.name
    WHERE u.deactivated = 0
      AND u.is_guest = 0
    ORDER BY u.name
    """

    user_rows = await room_store.db_pool.execute(
        "get_user_activity_users",
        users_query,
    )

    if not user_rows:
        return []

    users: Dict[str, Dict[str, Any]] = {}
    user_ids: List[str] = []
    for row in user_rows:
        user_id, display_name, last_login_ts = row
        users[user_id] = {
            "user_id": user_id,
            "display_name": display_name,
            "last_login_ts": last_login_ts or 0,
            "last_message_ts": 0,
            "courses": [],
        }
        user_ids.append(user_id)

    # 2. Get last message timestamp per user
    # We batch this for all users at once
    last_message_query = """
    SELECT
        e.sender,
        MAX(e.origin_server_ts) AS last_message_ts
    FROM events e
    WHERE e.type = 'm.room.message'
    GROUP BY e.sender
    """

    message_rows = await room_store.db_pool.execute(
        "get_user_activity_last_message",
        last_message_query,
    )

    for row in message_rows:
        sender, last_message_ts = row
        if sender in users:
            users[sender]["last_message_ts"] = last_message_ts or 0

    # 3. Get room memberships for all users, restricted to rooms with
    #    pangea.course_plan state event (i.e. course rooms) or
    #    pangea.activity_plan state event (activity rooms)
    #
    # We also get the course_plan and activity_plan content for each room
    # to determine which rooms are courses vs activities.
    memberships_query = """
    SELECT
        rm.user_id,
        rm.room_id,
        rm.membership
    FROM room_memberships rm
    INNER JOIN current_state_events cse_check
        ON cse_check.room_id = rm.room_id
    WHERE rm.membership = 'join'
      AND cse_check.type IN (?, ?)
    GROUP BY rm.user_id, rm.room_id, rm.membership
    """

    membership_rows = await room_store.db_pool.execute(
        "get_user_activity_memberships",
        memberships_query,
        PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
        PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    )

    # Collect all room_ids that users are members of
    all_room_ids: set = set()
    user_room_memberships: Dict[str, List[str]] = {}
    for row in membership_rows:
        user_id, room_id, _ = row
        all_room_ids.add(room_id)
        if user_id not in user_room_memberships:
            user_room_memberships[user_id] = []
        user_room_memberships[user_id].append(room_id)

    if not all_room_ids:
        return list(users.values())

    # 4. Get state events for all relevant rooms to determine
    #    which are courses vs activities and extract activity_id
    room_ids_list = list(all_room_ids)
    room_placeholders = ",".join(["?" for _ in room_ids_list])

    state_events_query = f"""
    SELECT
        cse.room_id,
        cse.type,
        cse.state_key,
        ej.json
    FROM current_state_events cse
    INNER JOIN event_json ej ON ej.event_id = cse.event_id
    WHERE cse.room_id IN ({room_placeholders})
      AND cse.type IN (?, ?)
    """

    state_params: Tuple[Any, ...] = (
        *room_ids_list,
        PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
        PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    )

    state_rows = await room_store.db_pool.execute(
        "get_user_activity_state_events",
        state_events_query,
        *state_params,
    )

    # Build room info map: room_id -> {is_course, is_activity, activity_id, room_name}
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

    # 5. Get room names for all relevant rooms
    name_placeholders = ",".join(["?" for _ in room_ids_list])
    room_names_query = f"""
    SELECT
        cse.room_id,
        ej.json
    FROM current_state_events cse
    INNER JOIN event_json ej ON ej.event_id = cse.event_id
    WHERE cse.room_id IN ({name_placeholders})
      AND cse.type = 'm.room.name'
    """

    name_rows = await room_store.db_pool.execute(
        "get_user_activity_room_names",
        room_names_query,
        *room_ids_list,
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

    # 6. For activity rooms, find which course (space) they belong to
    # via m.space.child / m.space.parent state events
    activity_room_ids = [
        rid for rid, info in room_info.items() if info.get("is_activity")
    ]

    activity_to_course: Dict[str, Optional[str]] = {}
    if activity_room_ids:
        activity_placeholders = ",".join(["?" for _ in activity_room_ids])

        # Look for m.space.parent state events in activity rooms
        # which point to their parent course
        parent_query = f"""
        SELECT
            cse.room_id,
            cse.state_key
        FROM current_state_events cse
        WHERE cse.room_id IN ({activity_placeholders})
          AND cse.type = 'm.space.parent'
        """

        parent_rows = await room_store.db_pool.execute(
            "get_user_activity_parents",
            parent_query,
            *activity_room_ids,
        )

        for row in parent_rows:
            activity_room_id, parent_room_id = row
            # Only assign if parent is actually a course
            if parent_room_id in room_info and room_info[parent_room_id].get(
                "is_course"
            ):
                activity_to_course[activity_room_id] = parent_room_id

    # 7. Get last message timestamp per user per room for course rooms
    # to determine "most recently active course"
    course_room_ids = [rid for rid, info in room_info.items() if info.get("is_course")]

    user_course_activity: Dict[str, Dict[str, int]] = {}
    if course_room_ids:
        course_placeholders = ",".join(["?" for _ in course_room_ids])

        course_activity_query = f"""
        SELECT
            e.sender,
            e.room_id,
            MAX(e.origin_server_ts) AS last_ts
        FROM events e
        WHERE e.type = 'm.room.message'
          AND e.room_id IN ({course_placeholders})
        GROUP BY e.sender, e.room_id
        """

        course_activity_rows = await room_store.db_pool.execute(
            "get_user_activity_course_activity",
            course_activity_query,
            *course_room_ids,
        )

        for row in course_activity_rows:
            sender, room_id, last_ts = row
            if sender not in user_course_activity:
                user_course_activity[sender] = {}
            user_course_activity[sender][room_id] = last_ts or 0

    # 8. Also get user message activity in child rooms of courses
    # to properly determine most recently active course
    # (users may be active in activity rooms within a course)
    if activity_room_ids:
        activity_placeholders = ",".join(["?" for _ in activity_room_ids])

        activity_msg_query = f"""
        SELECT
            e.sender,
            e.room_id,
            MAX(e.origin_server_ts) AS last_ts
        FROM events e
        WHERE e.type = 'm.room.message'
          AND e.room_id IN ({activity_placeholders})
        GROUP BY e.sender, e.room_id
        """

        activity_msg_rows = await room_store.db_pool.execute(
            "get_user_activity_in_activities",
            activity_msg_query,
            *activity_room_ids,
        )

        for row in activity_msg_rows:
            sender, activity_room_id, last_ts = row
            # Map activity room activity to parent course
            parent_course = activity_to_course.get(activity_room_id)
            if parent_course and sender in users:
                if sender not in user_course_activity:
                    user_course_activity[sender] = {}
                current = user_course_activity[sender].get(parent_course, 0)
                user_course_activity[sender][parent_course] = max(current, last_ts or 0)

    # 9. Assemble per-user course data
    for user_id, user_data in users.items():
        user_rooms = user_room_memberships.get(user_id, [])
        courses_list: List[Dict[str, Any]] = []

        for rid in user_rooms:
            info = room_info.get(rid)
            if not info:
                continue

            room_entry: Dict[str, Any] = {
                "room_id": rid,
                "room_name": room_names.get(rid),
                "is_course": info.get("is_course", False),
                "is_activity": info.get("is_activity", False),
                "activity_id": info.get("activity_id"),
                "parent_course_room_id": activity_to_course.get(rid),
            }
            courses_list.append(room_entry)

        # Determine most recently active course
        user_courses = user_course_activity.get(user_id, {})
        most_recent_course: Optional[str] = None
        most_recent_ts = 0
        for course_rid, ts in user_courses.items():
            if ts > most_recent_ts:
                most_recent_ts = ts
                most_recent_course = course_rid

        user_data["courses"] = courses_list
        user_data["most_recent_course_room_id"] = most_recent_course
        user_data["most_recent_course_activity_ts"] = most_recent_ts

    # 10. For each course, find activity rooms and their members
    # so the bot can determine eligible activity rooms to invite into.
    # We collect all activity rooms per course, with:
    # - activity_id
    # - room members
    # - creation timestamp (origin_server_ts of m.room.create)

    # Get creation timestamps for activity rooms
    activity_creation_ts: Dict[str, int] = {}
    if activity_room_ids:
        activity_placeholders = ",".join(["?" for _ in activity_room_ids])

        creation_query = f"""
        SELECT
            cse.room_id,
            e.origin_server_ts
        FROM current_state_events cse
        INNER JOIN events e ON e.event_id = cse.event_id
        WHERE cse.room_id IN ({activity_placeholders})
          AND cse.type = 'm.room.create'
        """

        creation_rows = await room_store.db_pool.execute(
            "get_user_activity_room_creation",
            creation_query,
            *activity_room_ids,
        )

        for row in creation_rows:
            room_id, ts = row
            activity_creation_ts[room_id] = ts or 0

    # Get members for activity rooms
    activity_members: Dict[str, List[str]] = {}
    if activity_room_ids:
        activity_placeholders = ",".join(["?" for _ in activity_room_ids])

        members_query = f"""
        SELECT
            rm.room_id,
            rm.user_id
        FROM room_memberships rm
        INNER JOIN current_state_events cse
            ON cse.event_id = rm.event_id
        WHERE rm.room_id IN ({activity_placeholders})
          AND rm.membership = 'join'
        """

        members_rows = await room_store.db_pool.execute(
            "get_user_activity_room_members",
            members_query,
            *activity_room_ids,
        )

        for row in members_rows:
            room_id, member_user_id = row
            if room_id not in activity_members:
                activity_members[room_id] = []
            activity_members[room_id].append(member_user_id)

    # Build activity_rooms_by_course: course_room_id -> list of activity room info
    activity_rooms_by_course: Dict[str, List[Dict[str, Any]]] = {}
    for activity_rid, course_rid in activity_to_course.items():
        if course_rid is None:
            continue
        if course_rid not in activity_rooms_by_course:
            activity_rooms_by_course[course_rid] = []

        info = room_info.get(activity_rid, {})
        activity_rooms_by_course[course_rid].append(
            {
                "room_id": activity_rid,
                "room_name": room_names.get(activity_rid),
                "activity_id": info.get("activity_id"),
                "members": activity_members.get(activity_rid, []),
                "created_ts": activity_creation_ts.get(activity_rid, 0),
            }
        )

    # Sort activity rooms by creation timestamp descending (most recent first)
    for course_rid in activity_rooms_by_course:
        activity_rooms_by_course[course_rid].sort(
            key=lambda x: x["created_ts"], reverse=True
        )

    result: List[Dict[str, Any]] = []
    for user_data in users.values():
        user_data["activity_rooms_by_course"] = activity_rooms_by_course
        result.append(user_data)

    return result
