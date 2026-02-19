from __future__ import annotations

import logging
import math
from typing import Any, Dict, List

from synapse.storage.databases.main.room import RoomStore

logger = logging.getLogger("synapse_pangea_chat.user_activity.get_users")


async def get_users(
    room_store: RoomStore,
    *,
    page: int = 1,
    limit: int = 50,
) -> Dict[str, Any]:
    """Return paginated list of local users with basic activity metadata.

    Response shape:
        {
            "docs": [ { user_id, display_name, last_login_ts,
                        last_message_ts } , … ],
            "page": 1,
            "limit": 50,
            "totalDocs": 200,
            "maxPage": 4,
        }

    Course memberships are available via the separate user_courses endpoint.
    """

    # --- 1. Count total users -------------------------------------------------
    count_query = """
    SELECT COUNT(*) FROM users u
    WHERE u.deactivated = 0 AND u.is_guest = 0
    """
    count_rows = await room_store.db_pool.execute("get_users_count", count_query)
    total_docs: int = count_rows[0][0] if count_rows else 0

    max_page = max(1, math.ceil(total_docs / limit))
    if page < 1:
        page = 1
    if page > max_page:
        page = max_page

    offset = (page - 1) * limit

    # --- 2. Fetch page of users with last login -------------------------------
    users_query = """
    WITH last_logins AS (
        SELECT user_id, MAX(last_seen) AS last_login_ts
        FROM user_ips
        GROUP BY user_id
    )
    SELECT
        u.name AS user_id,
        p.displayname AS display_name,
        COALESCE(ll.last_login_ts, 0) AS last_login_ts
    FROM users u
    LEFT JOIN profiles p ON p.full_user_id = u.name
    LEFT JOIN last_logins ll ON ll.user_id = u.name
    WHERE u.deactivated = 0
      AND u.is_guest = 0
    ORDER BY u.name
    LIMIT ? OFFSET ?
    """

    user_rows = await room_store.db_pool.execute(
        "get_users_page", users_query, limit, offset
    )

    if not user_rows:
        return {
            "docs": [],
            "page": page,
            "limit": limit,
            "totalDocs": total_docs,
            "maxPage": max_page,
        }

    users: List[Dict[str, Any]] = []
    user_ids: List[str] = []
    for row in user_rows:
        user_id, display_name, last_login_ts = row
        users.append(
            {
                "user_id": user_id,
                "display_name": display_name,
                "last_login_ts": last_login_ts or 0,
                "last_message_ts": 0,
            }
        )
        user_ids.append(user_id)

    user_placeholders = ",".join(["?" for _ in user_ids])

    # --- 3. Last message timestamp per user -----------------------------------
    last_message_query = f"""
    SELECT e.sender, MAX(e.origin_server_ts) AS last_message_ts
    FROM events e
    WHERE e.type = 'm.room.message'
      AND e.sender IN ({user_placeholders})
    GROUP BY e.sender
    """
    message_rows = await room_store.db_pool.execute(
        "get_users_last_message", last_message_query, *user_ids
    )
    users_by_id = {u["user_id"]: u for u in users}
    for row in message_rows:
        sender, last_message_ts = row
        if sender in users_by_id:
            users_by_id[sender]["last_message_ts"] = last_message_ts or 0

    return {
        "docs": users,
        "page": page,
        "limit": limit,
        "totalDocs": total_docs,
        "maxPage": max_page,
    }
