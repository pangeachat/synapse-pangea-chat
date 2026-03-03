"""DB-level user directory search with visibility filtering.

Moves the public-attribute and shared-room checks into a single SQL query
so that LIMIT applies *after* filtering rather than before.

Query structure and ranking are intentionally based on Synapse's
``UserDirectoryStore.search_user_dir`` in
``synapse/storage/databases/main/user_directory.py``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.user_directory_search.search_users"
)

# Safety cap on tsvector matches before applying visibility filter.
# Prevents full-table scans for very short prefixes like "t".
_CTE_MATCH_LIMIT = 10000

# Shared-room EXISTS subquery (used in multiple branches).
_SHARED_ROOM_CONDITION = """
    EXISTS (
        SELECT 1 FROM users_who_share_private_rooms spr
        WHERE spr.user_id = ? AND spr.other_user_id = d.user_id
    )
    OR EXISTS (
        SELECT 1 FROM users_in_public_rooms upr_req
        INNER JOIN users_in_public_rooms upr_other
            ON upr_req.room_id = upr_other.room_id
        WHERE upr_req.user_id = ? AND upr_other.user_id = d.user_id
    )
"""

# ── tsquery helpers ──────────────────────────────────────────────────


def _parse_search_term(search_term: str) -> List[str]:
    """Split a search term into words, matching Synapse's approach."""
    parts = re.split(r"[@:\s]+", search_term.strip().lower())
    return [p for p in parts if p]


def _escape_word(word: str) -> str:
    return "'" + word.replace("'", "''").replace("\\", "\\\\") + "'"


def _build_tsquery(words: List[str]) -> str:
    """Prefix-OR-exact per word, AND-ed together (Synapse full_query)."""
    return " & ".join(f"({_escape_word(w)}:* | {_escape_word(w)})" for w in words[:10])


def _build_exact_tsquery(words: List[str]) -> str:
    return " & ".join(_escape_word(w) for w in words[:10])


def _build_prefix_tsquery(words: List[str]) -> str:
    return " & ".join(f"{_escape_word(w)}:*" for w in words[:10])


# ── ORDER BY clause (shared by both branches) ───────────────────────

_ORDER_BY = """
    ORDER BY
        (CASE WHEN d.user_id IS NOT NULL THEN 4.0 ELSE 1.0 END)
        * (CASE WHEN d.display_name IS NOT NULL THEN 1.2 ELSE 1.0 END)
        * (CASE WHEN d.avatar_url IS NOT NULL THEN 1.2 ELSE 1.0 END)
        * (
            3 * ts_rank_cd(
                '{0.1, 0.1, 0.9, 1.0}',
                t.vector,
                to_tsquery('simple', ?),
                8
            )
            + ts_rank_cd(
                '{0.1, 0.1, 0.9, 1.0}',
                t.vector,
                to_tsquery('simple', ?),
                8
            )
        )
        * (CASE WHEN d.user_id LIKE ? THEN 2.0 ELSE 1.0 END)
        DESC,
        d.display_name IS NULL,
        d.avatar_url IS NULL
"""

_LOCKED_USER_FILTER = "(u.locked IS NULL OR u.locked = FALSE)"


# ── JSON extraction helper ───────────────────────────────────────────


def _build_json_extraction_expr(json_path: List[str]) -> str:
    """Build a PostgreSQL expression for extracting a value from account_data.

    Given path ``["profile", "user_settings", "public"]`` produces::

        (ad.content::jsonb -> 'user_settings' ->> 'public')

    The first element is the ``account_data_type`` (used in the JOIN
    condition) so the extraction starts from the second element.
    """
    if len(json_path) < 2:
        return "ad.content::jsonb"

    expr = "ad.content::jsonb"
    for key in json_path[1:-1]:
        expr = f"{expr} -> '{key}'"
    expr = f"{expr} ->> '{json_path[-1]}'"
    return f"({expr})"


# ── Public API ───────────────────────────────────────────────────────


async def search_users_db(
    db_pool: Any,
    *,
    requester_id: str,
    search_term: str,
    limit: int,
    server_name: str,
    public_attribute_json_path: List[str],
    filter_if_missing_public_attribute: bool,
    whitelist_requester_id_patterns: List[str],
    show_locked_users: bool,
) -> Dict[str, Any]:
    """Search the user directory with visibility filtering done in SQL.

    Returns ``{"limited": bool, "results": [{"user_id", "display_name", "avatar_url"}]}``.
    """
    is_whitelisted = any(
        re.match(pattern, requester_id) for pattern in whitelist_requester_id_patterns
    )

    words = _parse_search_term(search_term)
    if not words:
        return {"limited": False, "results": []}

    full_query = _build_tsquery(words)
    exact_query = _build_exact_tsquery(words)
    prefix_query = _build_prefix_tsquery(words)
    local_pattern = f"%:{server_name}"

    if is_whitelisted:
        return await _search_unfiltered(
            db_pool,
            requester_id=requester_id,
            full_query=full_query,
            exact_query=exact_query,
            prefix_query=prefix_query,
            limit=limit,
            local_pattern=local_pattern,
            show_locked_users=show_locked_users,
        )

    return await _search_filtered(
        db_pool,
        requester_id=requester_id,
        full_query=full_query,
        exact_query=exact_query,
        prefix_query=prefix_query,
        limit=limit,
        local_pattern=local_pattern,
        public_attribute_json_path=public_attribute_json_path,
        filter_if_missing_public_attribute=filter_if_missing_public_attribute,
        show_locked_users=show_locked_users,
    )


# ── Unfiltered search (whitelisted requesters) ──────────────────────


async def _search_unfiltered(
    db_pool: Any,
    *,
    requester_id: str,
    full_query: str,
    exact_query: str,
    prefix_query: str,
    limit: int,
    local_pattern: str,
    show_locked_users: bool,
) -> Dict[str, Any]:
    locked_where = "" if show_locked_users else f"AND {_LOCKED_USER_FILTER}"
    sql = f"""
        WITH matching_users AS (
            SELECT user_id, vector
            FROM user_directory_search
            WHERE vector @@ to_tsquery('simple', ?)
            LIMIT {_CTE_MATCH_LIMIT}
        )
        SELECT d.user_id, d.display_name, d.avatar_url
        FROM matching_users AS t
        INNER JOIN user_directory AS d USING (user_id)
        LEFT JOIN users AS u ON t.user_id = u.name
        WHERE d.user_id != ?
                    {locked_where}
        {_ORDER_BY}
        LIMIT ?
    """
    args = (
        full_query,
        requester_id,
        exact_query,
        prefix_query,
        local_pattern,
        limit + 1,
    )
    rows = await db_pool.execute("pangea_user_dir_search_unfiltered", sql, *args)
    return _to_response(rows, limit)


# ── Filtered search (normal requesters) ─────────────────────────────


async def _search_filtered(
    db_pool: Any,
    *,
    requester_id: str,
    full_query: str,
    exact_query: str,
    prefix_query: str,
    limit: int,
    local_pattern: str,
    public_attribute_json_path: List[str],
    filter_if_missing_public_attribute: bool,
    show_locked_users: bool,
) -> Dict[str, Any]:
    """Search with visibility filtering in SQL.

    Visibility rules (mirrors ``LimitUserDirectory.check_username_for_spam``):

    * Remote users (``user_id NOT LIKE '%:<server>'``): always visible.
    * Local + public attribute ``true``: always visible.
    * Local + missing public attribute: behaviour set by config flag.
    * Local + private (public != true): visible only via shared rooms.
    """
    account_data_type = public_attribute_json_path[0]
    json_expr = _build_json_extraction_expr(public_attribute_json_path)
    locked_where = "" if show_locked_users else f"AND {_LOCKED_USER_FILTER}"

    if filter_if_missing_public_attribute:
        missing_condition = _SHARED_ROOM_CONDITION
    else:
        missing_condition = "TRUE"

    sql = f"""
        WITH matching_users AS (
            SELECT user_id, vector
            FROM user_directory_search
            WHERE vector @@ to_tsquery('simple', ?)
            LIMIT {_CTE_MATCH_LIMIT}
        )
        SELECT d.user_id, d.display_name, d.avatar_url
        FROM matching_users AS t
        INNER JOIN user_directory AS d USING (user_id)
        LEFT JOIN users AS u ON t.user_id = u.name
        LEFT JOIN account_data ad
            ON ad.user_id = d.user_id
            AND ad.account_data_type = ?
        WHERE d.user_id != ?
                    {locked_where}
          AND (
            -- Remote users: always visible
            d.user_id NOT LIKE ?
            -- Public attribute is true
            OR LOWER({json_expr}) = 'true'
            -- Missing public attribute
            OR (
                {json_expr} IS NULL
                AND ({missing_condition})
            )
            -- Private users: attribute exists but is not true
            OR (
                {json_expr} IS NOT NULL
                AND LOWER({json_expr}) != 'true'
                AND ({_SHARED_ROOM_CONDITION})
            )
          )
        {_ORDER_BY}
        LIMIT ?
    """

    args: list[Any] = [
        full_query,
        account_data_type,
        requester_id,
        local_pattern,
    ]

    if filter_if_missing_public_attribute:
        # Params for the missing-attribute shared-room condition
        args.append(requester_id)
        args.append(requester_id)

    # Params for the private-users shared-room condition
    args.append(requester_id)
    args.append(requester_id)

    # ORDER BY params
    args.append(exact_query)
    args.append(prefix_query)
    args.append(local_pattern)

    args.append(limit + 1)

    rows = await db_pool.execute("pangea_user_dir_search_filtered", sql, *args)
    return _to_response(rows, limit)


# ── helpers ──────────────────────────────────────────────────────────


def _to_response(rows: list, limit: int) -> Dict[str, Any]:
    limited = len(rows) > limit
    results = [
        {
            "user_id": row[0],
            "display_name": row[1],
            "avatar_url": row[2],
        }
        for row in rows[:limit]
    ]
    return {"limited": limited, "results": results}
