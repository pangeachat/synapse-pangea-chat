from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Set

from synapse.module_api import ModuleApi
from synapse.storage.databases.main.room import RoomStore

logger = logging.getLogger("synapse_pangea_chat.user_activity.get_users")

USER_ACTIVITY_SORT_BY_VALUES = {
    "user_id",
    "last_login_ts",
    "last_message_ts",
    "latest_activity",
}
USER_ACTIVITY_SORT_ORDER_VALUES = {"asc", "desc"}
DEFAULT_USER_ACTIVITY_SORT_BY = "user_id"
DEFAULT_USER_ACTIVITY_SORT_ORDER = "asc"


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
    sort_by: str = DEFAULT_USER_ACTIVITY_SORT_BY,
    sort_order: str = DEFAULT_USER_ACTIVITY_SORT_ORDER,
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

    Sort params are opt-in and non-breaking. Defaults preserve the historical
    user_id ascending order. latest_activity means max(last_login_ts,
    last_message_ts).

    When user_ids and course_ids are both provided the result is their intersection.

    totalDocs and maxPage always reflect the fully-filtered count.

    Response shape:
        {
            "docs": [ { user_id, display_name, last_login_ts,
                        last_message_ts, latest_activity_ts } , … ],
            "page": 1,
            "limit": 50,
            "totalDocs": 200,
            "maxPage": 4,
        }

    Course memberships are available via the separate user_courses endpoint.
    """
    sort_by = (
        sort_by
        if sort_by in USER_ACTIVITY_SORT_BY_VALUES
        else DEFAULT_USER_ACTIVITY_SORT_BY
    )
    sort_order = (
        sort_order
        if sort_order in USER_ACTIVITY_SORT_ORDER_VALUES
        else DEFAULT_USER_ACTIVITY_SORT_ORDER
    )

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

    def _activity_ctes() -> str:
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
            {_activity_ctes()}
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

        needs_message_before_pagination = needs_inactive_ctes or sort_by in {
            "last_message_ts",
            "latest_activity",
        }

        if needs_message_before_pagination:
            users_query = f"""
            {_activity_ctes()}
            SELECT
                u.name AS user_id,
                p.displayname AS display_name,
                COALESCE(ll.last_login_ts, 0) AS last_login_ts,
                COALESCE(lm.last_message_ts, 0) AS last_message_ts,
                GREATEST(
                    COALESCE(ll.last_login_ts, 0),
                    COALESCE(lm.last_message_ts, 0)
                ) AS latest_activity_ts
            FROM users u
            LEFT JOIN profiles p ON p.full_user_id = u.name
            LEFT JOIN last_logins ll ON ll.user_id = u.name
            LEFT JOIN last_messages lm ON lm.sender = u.name
            WHERE u.deactivated = 0 AND u.is_guest = 0{extra_where}
            ORDER BY {_sql_order_by(sort_by, sort_order)}
            LIMIT ? OFFSET ?
            """
            user_rows = await room_store.db_pool.execute(
                "get_users_page", users_query, *filter_args, limit, offset
            )
            users = [_user_from_activity_row(row) for row in user_rows]
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
            ORDER BY {_sql_login_only_order_by(sort_by, sort_order)}
            LIMIT ? OFFSET ?
            """
            user_rows = await room_store.db_pool.execute(
                "get_users_page", users_query, *filter_args, limit, offset
            )
            users = [_user_from_login_row(row) for row in user_rows]
            await _attach_page_last_messages(room_store, users)

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
    # 1. Fetch all matching candidate user docs (SQL-filtered, no LIMIT)
    extra_where, filter_args = _build_extra_where()

    candidates_query = f"""
    {_activity_ctes()}
    SELECT
        u.name AS user_id,
        p.displayname AS display_name,
        COALESCE(ll.last_login_ts, 0) AS last_login_ts,
        COALESCE(lm.last_message_ts, 0) AS last_message_ts,
        GREATEST(
            COALESCE(ll.last_login_ts, 0),
            COALESCE(lm.last_message_ts, 0)
        ) AS latest_activity_ts
    FROM users u
    LEFT JOIN profiles p ON p.full_user_id = u.name
    LEFT JOIN last_logins ll ON ll.user_id = u.name
    LEFT JOIN last_messages lm ON lm.sender = u.name
    WHERE u.deactivated = 0 AND u.is_guest = 0{extra_where}
    ORDER BY {_sql_order_by(sort_by, sort_order)}
    """

    candidate_rows = await room_store.db_pool.execute(
        "get_users_candidates", candidates_query, *filter_args
    )
    candidate_users = [_user_from_activity_row(row) for row in candidate_rows]

    # 2. Filter by notification cooldown
    cooldown_threshold_ms = int(time.time() * 1000) - notification_cooldown_ms
    filtered_users: List[Dict[str, Any]] = []

    for user in candidate_users:
        uid = user["user_id"]
        recently_notified = await _user_recently_notified(
            room_store=room_store,
            api=api,
            user_id=uid,
            bot_user_id=bot_user_id,
            cooldown_threshold_ms=cooldown_threshold_ms,
        )
        if not recently_notified:
            filtered_users.append(user)

    filtered_users = _sort_user_docs(
        filtered_users,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    # 3. Paginate in Python now that we have the exact filtered list
    total_docs = len(filtered_users)
    max_page = max(1, math.ceil(total_docs / limit))
    if page < 1:
        page = 1
    if page > max_page:
        page = max_page

    page_users = filtered_users[(page - 1) * limit : page * limit]

    return {
        "docs": page_users,
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

    placeholders = ",".join(["?" for _ in bot_dm_rooms])
    query = f"""
        SELECT 1
        FROM events e
        WHERE e.room_id IN ({placeholders})
          AND e.sender = ?
          AND e.type = 'p.room.notice'
          AND e.origin_server_ts > ?
        LIMIT 1
    """
    rows = await room_store.db_pool.execute(
        "get_users_recent_bot_notice",
        query,
        *bot_dm_rooms,
        bot_user_id,
        cooldown_threshold_ms,
    )
    return bool(rows)


def _sql_order_by(sort_by: str, sort_order: str) -> str:
    direction = _sql_direction(sort_order)
    if sort_by == "user_id":
        return f"u.name {direction}"
    if sort_by == "last_login_ts":
        return f"COALESCE(ll.last_login_ts, 0) {direction}, u.name ASC"
    if sort_by == "last_message_ts":
        return f"COALESCE(lm.last_message_ts, 0) {direction}, u.name ASC"
    if sort_by == "latest_activity":
        return (
            "GREATEST(COALESCE(ll.last_login_ts, 0), "
            f"COALESCE(lm.last_message_ts, 0)) {direction}, u.name ASC"
        )
    raise ValueError(f"Unsupported user_activity sort_by: {sort_by}")


def _sql_login_only_order_by(sort_by: str, sort_order: str) -> str:
    direction = _sql_direction(sort_order)
    if sort_by == "user_id":
        return f"u.name {direction}"
    if sort_by == "last_login_ts":
        return f"COALESCE(ll.last_login_ts, 0) {direction}, u.name ASC"
    raise ValueError(f"Unsupported login-only user_activity sort_by: {sort_by}")


def _sql_direction(sort_order: str) -> str:
    if sort_order == "asc":
        return "ASC"
    if sort_order == "desc":
        return "DESC"
    raise ValueError(f"Unsupported user_activity sort_order: {sort_order}")


def _user_from_activity_row(row: Any) -> Dict[str, Any]:
    uid, display_name, last_login_ts, last_message_ts, latest_activity_ts = row
    return {
        "user_id": uid,
        "display_name": display_name,
        "last_login_ts": last_login_ts or 0,
        "last_message_ts": last_message_ts or 0,
        "latest_activity_ts": latest_activity_ts or 0,
    }


def _user_from_login_row(row: Any) -> Dict[str, Any]:
    uid, display_name, last_login_ts = row
    return {
        "user_id": uid,
        "display_name": display_name,
        "last_login_ts": last_login_ts or 0,
        "last_message_ts": 0,
        "latest_activity_ts": last_login_ts or 0,
    }


async def _attach_page_last_messages(
    room_store: RoomStore,
    users: List[Dict[str, Any]],
) -> None:
    if not users:
        return

    page_user_ids = [user["user_id"] for user in users]
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

    for user in users:
        user["latest_activity_ts"] = max(
            int(user.get("last_login_ts") or 0),
            int(user.get("last_message_ts") or 0),
        )


def _sort_user_docs(
    users: List[Dict[str, Any]],
    *,
    sort_by: str,
    sort_order: str,
) -> List[Dict[str, Any]]:
    if sort_by == "user_id":
        return sorted(
            users,
            key=lambda user: str(user["user_id"]),
            reverse=sort_order == "desc",
        )

    value_key = "latest_activity_ts" if sort_by == "latest_activity" else sort_by
    reverse_primary = sort_order == "desc"
    return sorted(
        users,
        key=lambda user: (
            -int(user.get(value_key) or 0)
            if reverse_primary
            else int(user.get(value_key) or 0),
            str(user["user_id"]),
        ),
    )
