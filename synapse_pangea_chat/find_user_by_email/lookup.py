"""Resolve an email address to the local accounts that have bound it.

Case-insensitive on both sides: an address bound years ago may be stored in
whatever casing the user typed, and a lookup that missed it would report
"no account" for a real user.

``LOWER(address)`` cannot use the ``user_threepids_medium_address`` index, so
this is a sequential scan of ``user_threepids``. That table holds roughly one
row per user with an email, which is small enough that the scan is cheaper than
maintaining a functional index for an admin-only endpoint.
"""

from __future__ import annotations

from typing import Any, Dict, List

EMAIL_MEDIUM = "email"

# An address maps to more than one account only in duplicate-registration or
# shared-mailbox cases, so anything past this is pathological. The cap keeps a
# runaway result set from being serialised into a response.
MAX_RESULTS = 100

_LOOKUP_SQL = """
    SELECT t.user_id, t.address, p.displayname, u.deactivated
    FROM user_threepids AS t
    INNER JOIN users AS u ON u.name = t.user_id
    LEFT JOIN profiles AS p ON p.full_user_id = t.user_id
    WHERE t.medium = ?
      AND LOWER(t.address) = LOWER(?)
    ORDER BY t.user_id
    LIMIT ?
"""


async def find_users_by_email_db(db_pool: Any, address: str) -> List[Dict[str, Any]]:
    """Return one entry per account holding ``address`` as a verified email.

    Each entry carries ``user_id``, the ``address`` as stored (which may differ
    in casing from the query), ``display_name``, and ``deactivated``.
    """
    rows = await db_pool.execute(
        "pangea_find_user_by_email",
        _LOOKUP_SQL,
        EMAIL_MEDIUM,
        address,
        MAX_RESULTS,
    )
    return [
        {
            "user_id": row[0],
            "address": row[1],
            "display_name": row[2],
            "deactivated": bool(row[3]),
        }
        for row in rows
    ]
