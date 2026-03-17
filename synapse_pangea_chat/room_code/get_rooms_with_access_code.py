from typing import List, NamedTuple

from synapse.storage.databases.main.room import RoomStore


class RoomCodeMatch(NamedTuple):
    room_id: str
    is_admin_code: bool


async def get_rooms_with_access_code(
    access_code: str, room_store: RoomStore
) -> List[RoomCodeMatch]:
    """
    Query the Synapse database for rooms that have a state event `m.room.join_rules`
    with content that includes the provided access code in either the `access_code`
    or `admin_access_code` field.

    :param access_code: The access code to search for.
    :return: A List of RoomCodeMatch(room_id, is_admin_code) tuples.
    """
    database_engine = room_store.db_pool.engine.module.__name__

    if "sqlite" in database_engine:
        query = """
            SELECT cse.room_id,
                   CASE
                     WHEN LOWER(json_extract(ej.json, '$.content.admin_access_code')) = LOWER(?) THEN 1
                     ELSE 0
                   END AS is_admin
            FROM current_state_events cse
                JOIN events e ON cse.event_id = e.event_id
                JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                cse.type = 'm.room.join_rules'
                AND (
                    LOWER(json_extract(ej.json, '$.content.access_code')) = LOWER(?)
                    OR LOWER(json_extract(ej.json, '$.content.admin_access_code')) = LOWER(?)
                )
            """
        params = (access_code, access_code, access_code)

    else:
        query = """
            SELECT cse.room_id,
                   CASE
                     WHEN LOWER((ej.json::jsonb)->'content'->>'admin_access_code') = LOWER(%s) THEN true
                     ELSE false
                   END AS is_admin
            FROM current_state_events cse
            JOIN events e ON cse.event_id = e.event_id
            JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                cse.type = 'm.room.join_rules'
                AND (
                    LOWER((ej.json::jsonb)->'content'->>'access_code') = LOWER(%s)
                    OR LOWER((ej.json::jsonb)->'content'->>'admin_access_code') = LOWER(%s)
                )
            ;
            """
        params = (access_code, access_code, access_code)

    rows = await room_store.db_pool.execute(
        "get_rooms_with_access_code",
        query,
        *params,
    )
    results: List[RoomCodeMatch] = []
    for row in rows:
        if isinstance(row, tuple) and len(row) >= 2:
            results.append(RoomCodeMatch(room_id=row[0], is_admin_code=bool(row[1])))
        elif isinstance(row, str):
            results.append(RoomCodeMatch(room_id=row, is_admin_code=False))
    return results
