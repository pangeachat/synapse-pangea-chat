from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Set

from synapse.module_api import ModuleApi
from synapse.storage.databases.main.room import RoomStore

logger = logging.getLogger("synapse_pangea_chat.user_activity.get_users")


async def get_users(
    room_store: RoomStore,
    *,
    page: int = 1,
    limit: int = 50,
    user_ids: Optional[List[str]] = None,
    course_ids: Optional[List[str]] = None,
    inactive_days: Optional[int] = None,
    notification_cooldown_ms: Optional[int] = None,
    bot_user_id: Optional[str] = None,
    api: Optional[ModuleApi] = None,
) -> Dict[str, Any]:
    """Return paginated list of local users with basic activity metadata.

    Filter params (all optional, composable):
      user_ids               - restrict to these user IDs
      course_ids             - restrict to members of these course rooms
      inactive_days          - only users where max(last_login_ts, last_message_ts)
                               is older than this many days, or who have no activity
      notification_cooldown_ms - exclude users who have a p.room.notice from
                               bot_user_id in their bot DM room within the last N ms.
                               Requires api and bot_user_id to be set.
                               WARNING: performs O(N candidates) account-data lookups
                               when no user_ids/course_ids narrow the set.

    When user_ids and course_ids are both provided the result is their intersection.

    totalDocs and maxPage always reflect the fully-filtered count.

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

    # ------------------------------------------------------------------
    # Step 1 — resolve id_filter from user_ids / course_ids
    # ------------------------------------------------------------------
    id_filter: Optional[Set[str]] = None

    if course_ids:
        course_placeholders = ",".join(["?" for _ in course_ids])
        course_members_query = f"""
        SELECT DISTINCT rm.user_id
        FROM room_memberships rm
        INNER JOIN current_state_events cse ON cse.event_id = rm.event_id
        WHERE rm.room_id IN ({course_placeholders})
          AND rm.membership = 'join'
        """
        course_member_rows = await room_store.db_pool.execute(
            "get_users_course_members", course_members_query, *course_ids
        )
        course_member_ids: Set[str] = {row[0] for row in course_member_rows}

        if user_ids is not None:
            # intersection
            id_filter = course_member_ids & set(user_ids)
        else:
            id_filter = course_member_ids
    elif user_ids is not None:
        id_filter = set(user_ids)
    # else id_filter remains None → no ID filter

    # ------------------------------------------------------------------
    # Step 2 — build shared WHERE clause and CTE fragments
    # ------------------------------------------------------------------
    # inactive_days threshold: epoch-ms before which last activity must fall
    inactivity_threshold_ms: Optional[int] = None
    if inactive_days is not None:
        inactivity_threshold_ms = int(time.time() * 1000) - inactive_days * 86_400_000

    def _build_extra_where() -> tuple[str, list[Any]]:
        """Return (extra WHERE snippet, positional args list).

        The snippet begins with ' AND ' when non-empty so it can be appended
        directly after the base ``WHERE u.deactivated = 0 AND u.is_guest = 0``.
        """
        clauses: list[str] = []
        args: list[Any] = []

        if id_filter is not None:
            if not id_filter:
                # Empty filter → guaranteed zero results
                clauses.append("1 = 0")
            else:
                placeholders = ",".join(["?" for _ in id_filter])
                clauses.append(f"u.name IN ({placeholders})")
                args.extend(sorted(id_filter))  # sort for stable query plans

        if inactivity_threshold_ms is not None:
            clauses.append(
                "COALESCE(ll.last_login_ts, 0) < ?"
                " AND COALESCE(lm.last_message_ts, 0) < ?"
            )
            args.extend([inactivity_threshold_ms, inactivity_threshold_ms])

        if clauses:
            return " AND " + " AND ".join(clauses), args
        return "", args

    needs_inactive_ctes = inactivity_threshold_ms is not None

    def _inactive_ctes() -> str:
        return """
    WITH last_logins AS (
        SELECT user_id, MAX(last_seen) AS last_login_ts
        FROM user_ips
        GROUP BY user_id
    ),
    last_messages AS (
        SELECT sender, MAX(origin_server_ts) AS last_message_ts
        FROM events
        WHERE type = 'm.room.message'
        GROUP BY sender
    )
    """

    def _login_cte() -> str:
        return """
    WITH last_logins AS (
        SELECT user_id, MAX(last_seen) AS last_login_ts
        FROM user_ips
        GROUP BY user_id
    )
    """

    # ------------------------------------------------------------------
    # Path A — no notification_cooldown_ms: SQL handles count + pagination
    # ------------------------------------------------------------------
    if notification_cooldown_ms is None:
        extra_where, filter_args = _build_extra_where()

        if needs_inactive_ctes:
            count_query = f"""
            {_inactive_ctes()}
            SELECT COUNT(*)
            FROM users u
            LEFT JOIN last_logins ll ON ll.user_id = u.name
            LEFT JOIN last_messages lm ON lm.sender = u.name
            WHERE u.deactivated = 0 AND u.is_guest = 0{extra_where}
            """
        else:
            count_query = f"""
            SELECT COUNT(*)
            FROM users u
            WHERE u.deactivated = 0 AND u.is_guest = 0{extra_where}
            """

        count_rows = await room_store.db_pool.execute(
            "get_users_count", count_query, *filter_args
        )
        total_docs: int = count_rows[0][0] if count_rows else 0

        max_page = max(1, math.ceil(total_docs / limit))
        if page < 1:
            page = 1
        if page > max_page:
            page = max_page
        offset = (page - 1) * limit

        if needs_inactive_ctes:
            users_query = f"""
            {_inactive_ctes()}
            SELECT
                u.name AS user_id,
                p.displayname AS display_name,
                COALESCE(ll.last_login_ts, 0) AS last_login_ts,
                COALESCE(lm.last_message_ts, 0) AS last_message_ts
            FROM users u
            LEFT JOIN profiles p ON p.full_user_id = u.name
            LEFT JOIN last_logins ll ON ll.user_id = u.name
            LEFT JOIN last_messages lm ON lm.sender = u.name
            WHERE u.deactivated = 0 AND u.is_guest = 0{extra_where}
            ORDER BY u.name
            LIMIT ? OFFSET ?
            """
            user_rows = await room_store.db_pool.execute(
                "get_users_page", users_query, *filter_args, limit, offset
            )
        else:
            users_query = f"""
            {_login_cte()}
            SELECT
                u.name AS user_id,
                p.displayname AS display_name,
                COALESCE(ll.last_login_ts, 0) AS last_login_ts
            FROM users u
            LEFT JOIN profiles p ON p.full_user_id = u.name
            LEFT JOIN last_logins ll ON ll.user_id = u.name
            WHERE u.deactivated = 0 AND u.is_guest = 0{extra_where}
            ORDER BY u.name
            LIMIT ? OFFSET ?
            """
            user_rows = await room_store.db_pool.execute(
                "get_users_page", users_query, *filter_args, limit, offset
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
        page_user_ids: List[str] = []

        if needs_inactive_ctes:
            for row in user_rows:
                uid, display_name, last_login_ts, last_message_ts = row
                users.append(
                    {
                        "user_id": uid,
                        "display_name": display_name,
                        "last_login_ts": last_login_ts or 0,
                        "last_message_ts": last_message_ts or 0,
                    }
                )
                page_user_ids.append(uid)
        else:
            for row in user_rows:
                uid, display_name, last_login_ts = row
                users.append(
                    {
                        "user_id": uid,
                        "display_name": display_name,
                        "last_login_ts": last_login_ts or 0,
                        "last_message_ts": 0,
                    }
                )
                page_user_ids.append(uid)

            # Fetch last message ts separately (path A without inactive filter)
            user_placeholders = ",".join(["?" for _ in page_user_ids])
            last_message_query = f"""
            SELECT e.sender, MAX(e.origin_server_ts) AS last_message_ts
            FROM events e
            WHERE e.type = 'm.room.message'
              AND e.sender IN ({user_placeholders})
            GROUP BY e.sender
            """
            message_rows = await room_store.db_pool.execute(
                "get_users_last_message", last_message_query, *page_user_ids
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

    # ------------------------------------------------------------------
    # Path B — notification_cooldown_ms: full-scan then per-user filter
    # ------------------------------------------------------------------
    # 1. Fetch all matching candidate user IDs (SQL-filtered, no LIMIT)
    extra_where, filter_args = _build_extra_where()

    if needs_inactive_ctes:
        candidates_query = f"""
        {_inactive_ctes()}
        SELECT u.name
        FROM users u
        LEFT JOIN last_logins ll ON ll.user_id = u.name
        LEFT JOIN last_messages lm ON lm.sender = u.name
        WHERE u.deactivated = 0 AND u.is_guest = 0{extra_where}
        ORDER BY u.name
        """
    else:
        candidates_query = f"""
        SELECT u.name
        FROM users u
        WHERE u.deactivated = 0 AND u.is_guest = 0{extra_where}
        ORDER BY u.name
        """

    candidate_rows = await room_store.db_pool.execute(
        "get_users_candidates", candidates_query, *filter_args
    )
    candidate_ids: List[str] = [row[0] for row in candidate_rows]

    # 2. Filter by notification cooldown
    cooldown_threshold_ms = int(time.time() * 1000) - notification_cooldown_ms
    filtered_ids: List[str] = []

    for uid in candidate_ids:
        recently_notified = await _user_recently_notified(
            room_store=room_store,
            api=api,
            user_id=uid,
            bot_user_id=bot_user_id,
            cooldown_threshold_ms=cooldown_threshold_ms,
        )
        if not recently_notified:
            filtered_ids.append(uid)

    # 3. Paginate in Python now that we have the exact filtered list
    total_docs = len(filtered_ids)
    max_page = max(1, math.ceil(total_docs / limit))
    if page < 1:
        page = 1
    if page > max_page:
        page = max_page

    page_ids = filtered_ids[(page - 1) * limit : page * limit]

    if not page_ids:
        return {
            "docs": [],
            "page": page,
            "limit": limit,
            "totalDocs": total_docs,
            "maxPage": max_page,
        }

    # 4. Fetch display_name + last_login_ts + last_message_ts for page_ids
    page_placeholders = ",".join(["?" for _ in page_ids])
    page_users_query = f"""
    {_login_cte()}
    SELECT
        u.name AS user_id,
        p.displayname AS display_name,
        COALESCE(ll.last_login_ts, 0) AS last_login_ts
    FROM users u
    LEFT JOIN profiles p ON p.full_user_id = u.name
    LEFT JOIN last_logins ll ON ll.user_id = u.name
    WHERE u.name IN ({page_placeholders})
    ORDER BY u.name
    """
    page_user_rows = await room_store.db_pool.execute(
        "get_users_page_b", page_users_query, *page_ids
    )

    users = []
    for row in page_user_rows:
        uid, display_name, last_login_ts = row
        users.append(
            {
                "user_id": uid,
                "display_name": display_name,
                "last_login_ts": last_login_ts or 0,
                "last_message_ts": 0,
            }
        )

    # Fetch last message ts
    last_message_query = f"""
    SELECT e.sender, MAX(e.origin_server_ts) AS last_message_ts
    FROM events e
    WHERE e.type = 'm.room.message'
      AND e.sender IN ({page_placeholders})
    GROUP BY e.sender
    """
    message_rows = await room_store.db_pool.execute(
        "get_users_last_message_b", last_message_query, *page_ids
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


async def _user_recently_notified(
    *,
    room_store: RoomStore,
    api: Optional[ModuleApi],
    user_id: str,
    bot_user_id: Optional[str],
    cooldown_threshold_ms: int,
) -> bool:
    """Return True if the user has a p.room.notice from bot_user_id in their
    bot DM room with an origin_server_ts > cooldown_threshold_ms.

    Returns False (do not exclude) when api/bot_user_id are None or when no
    bot DM rooms are found.
    """
    if api is None or not bot_user_id:
        return False

    try:
        m_direct = await api.account_data_manager.get_global(user_id, "m.direct")
    except Exception:
        logger.debug(
            "Could not fetch m.direct account data for %s, treating as not notified",
            user_id,
        )
        return False

    if not m_direct:
        return False

    bot_dm_rooms: List[str] = m_direct.get(bot_user_id, [])
    if not bot_dm_rooms:
        return False

    room_placeholders = ",".join(["?" for _ in bot_dm_rooms])
    notice_query = f"""
    SELECT 1 FROM events
    WHERE room_id IN ({room_placeholders})
      AND sender = ?
      AND type = 'p.room.notice'
      AND origin_server_ts > ?
    LIMIT 1
    """
    notice_rows = await room_store.db_pool.execute(
        "get_users_recent_notice",
        notice_query,
        *bot_dm_rooms,
        bot_user_id,
        cooldown_threshold_ms,
    )
    return bool(notice_rows)
