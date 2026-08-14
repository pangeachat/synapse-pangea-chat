"""Resolve an access code to the room(s) that carry it.

Shared by ``knock_with_code``, ``preview_with_code``, ``request_room_code`` and
``create_course_space``.

Two paths, and which one runs depends only on whether the module-owned index
has finished backfilling (see ``access_code_index``):

* **Indexed** — probe the index for candidate rooms, then confirm each against
  ``current_state_events``. Cost is proportional to the number of matches,
  which is normally zero or one.
* **Scan** — the original query: extract the code out of every room's
  join-rules JSON and compare. O(total rooms) per call; it is what made this
  endpoint 12 s at 50 VU on staging (issue #163). Kept as the fallback so that
  turning the index off, or running before its backfill lands, is a slowdown
  rather than an outage.

Both paths answer identically. The index only narrows what gets checked; the
answer always comes from Synapse's own current state.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, NamedTuple

from synapse.storage.databases.main.room import RoomStore

from synapse_pangea_chat.room_code.access_code_index import (
    extract_codes,
    index_is_ready,
    lookup_candidate_room_ids,
)
from synapse_pangea_chat.room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.get_rooms_with_access_code"
)


class RoomCodeMatch(NamedTuple):
    room_id: str
    is_admin_code: bool


async def get_rooms_with_access_code(
    access_code: str, room_store: RoomStore
) -> List[RoomCodeMatch]:
    """
    Find the rooms whose current `m.room.join_rules` state carries `access_code`
    in either the `access_code` or the `admin_access_code` field, compared
    case-insensitively.

    :param access_code: The access code to search for.
    :return: A List of RoomCodeMatch(room_id, is_admin_code) tuples.
    """
    if await index_is_ready(room_store):
        try:
            candidate_room_ids = await lookup_candidate_room_ids(
                room_store, access_code
            )
            if not candidate_room_ids:
                return []
            return await _confirm_candidates(
                access_code, candidate_room_ids, room_store
            )
        except Exception as e:
            # Anything wrong with the index is a performance problem, not a
            # reason a student can't get into their class. Fall through to the
            # query that needs nothing but Synapse's own tables.
            logger.warning("access code index lookup failed, falling back: %s", e)

    return await _scan_every_room(access_code, room_store)


async def _confirm_candidates(
    access_code: str,
    candidate_room_ids: List[str],
    room_store: RoomStore,
) -> List[RoomCodeMatch]:
    """Re-read the candidates' join rules and keep the ones that really match.

    This is what lets a stale index row be harmless: a room whose code has
    since changed, or whose admin code was burned, or that was purged
    altogether, simply fails to confirm.
    """
    placeholders = ",".join("?" for _ in candidate_room_ids)
    query = f"""
        SELECT cse.room_id, ej.json
        FROM current_state_events cse
        JOIN event_json ej ON ej.event_id = cse.event_id
        WHERE cse.type = ?
          AND cse.room_id IN ({placeholders})
        """

    rows = await room_store.db_pool.execute(
        "confirm_rooms_with_access_code",
        query,
        EVENT_TYPE_M_ROOM_JOIN_RULES,
        *candidate_room_ids,
    )

    wanted = access_code.lower()
    results: List[RoomCodeMatch] = []
    for room_id, event_json in rows or []:
        content = _join_rules_content(room_id, event_json)
        codes = extract_codes(content)
        if codes.admin_access_code_lower == wanted:
            results.append(RoomCodeMatch(room_id=room_id, is_admin_code=True))
        elif codes.access_code_lower == wanted:
            results.append(RoomCodeMatch(room_id=room_id, is_admin_code=False))
    return results


def _join_rules_content(room_id: str, event_json: Any) -> Any:
    try:
        event = json.loads(event_json) if isinstance(event_json, str) else event_json
    except ValueError:
        logger.warning("unparseable join rules event for room %s", room_id)
        return None
    return event.get("content") if isinstance(event, dict) else None


async def _scan_every_room(
    access_code: str, room_store: RoomStore
) -> List[RoomCodeMatch]:
    """The pre-index query: extract and compare across every room."""
    database_engine = room_store.db_pool.engine.module.__name__

    if "sqlite" in database_engine:
        query = f"""
            SELECT cse.room_id,
                   CASE
                     WHEN LOWER(json_extract(ej.json, '$.content.{ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY}')) = LOWER(?) THEN 1
                     ELSE 0
                   END AS is_admin
            FROM current_state_events cse
                JOIN events e ON cse.event_id = e.event_id
                JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                cse.type = '{EVENT_TYPE_M_ROOM_JOIN_RULES}'
                AND (
                    LOWER(json_extract(ej.json, '$.content.{ACCESS_CODE_JOIN_RULE_CONTENT_KEY}')) = LOWER(?)
                    OR LOWER(json_extract(ej.json, '$.content.{ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY}')) = LOWER(?)
                )
            """
    else:
        query = f"""
            SELECT cse.room_id,
                   CASE
                     WHEN LOWER((ej.json::jsonb)->'content'->>'{ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY}') = LOWER(?) THEN true
                     ELSE false
                   END AS is_admin
            FROM current_state_events cse
            JOIN events e ON cse.event_id = e.event_id
            JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                cse.type = '{EVENT_TYPE_M_ROOM_JOIN_RULES}'
                AND (
                    LOWER((ej.json::jsonb)->'content'->>'{ACCESS_CODE_JOIN_RULE_CONTENT_KEY}') = LOWER(?)
                    OR LOWER((ej.json::jsonb)->'content'->>'{ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY}') = LOWER(?)
                )
            """

    rows = await room_store.db_pool.execute(
        "get_rooms_with_access_code",
        query,
        access_code,
        access_code,
        access_code,
    )
    results: List[RoomCodeMatch] = []
    for row in rows:
        if isinstance(row, tuple) and len(row) >= 2:
            results.append(RoomCodeMatch(room_id=row[0], is_admin_code=bool(row[1])))
        elif isinstance(row, str):
            results.append(RoomCodeMatch(room_id=row, is_admin_code=False))
    return results
