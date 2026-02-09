from typing import List

from synapse.storage.databases.main.room import RoomStore


async def get_rooms_with_access_code(
    access_code: str, room_store: RoomStore
) -> List[str]:
    """
    Query the Synapse database for rooms that have a state event `m.room.join_rules`
    with content that includes the provided access code.

    :param access_code: The access code to search for.
    :return: A List of room IDs where the `access_code` matches. None if there was an error
    """
    # Execute the query and retrieve room IDs
    # Check which database backend we are using
    database_engine = room_store.db_pool.engine.module.__name__

    if "sqlite" in database_engine:
        # SQLite: use json_extract and make comparison case-insensitive
        query = """
            SELECT e.room_id
            FROM events e
                JOIN state_events se ON e.event_id = se.event_id
                JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                e.type = 'm.room.join_rules'
                AND se.room_id = e.room_id
                AND se.type = 'm.room.join_rules'
                AND LOWER(json_extract(ej.json, '$.content.access_code')) = LOWER(?)
            GROUP BY se.room_id
            HAVING MAX(e.origin_server_ts)
            """
        params = (access_code,)  # Use a List with placeholders

    else:
        # PostgreSQL: use jsonb_extract_path_text and make comparison case-insensitive
        query = """
            SELECT DISTINCT ON (e.room_id) e.room_id, e.event_id
            FROM events e
            JOIN state_events se ON e.event_id = se.event_id
            JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                e.type = 'm.room.join_rules'
                AND se.type = 'm.room.join_rules'
                AND LOWER((ej.json::jsonb)->'content'->>'access_code') = LOWER(%s)
            ORDER BY e.room_id, e.origin_server_ts DESC;
            """
        params = (access_code,)  # Use a tuple with placeholders

    rows = await room_store.db_pool.execute(
        "get_rooms_with_access_code",
        query,
        *params,
    )
    room_ids: List[str] = []
    for row in rows:
        if isinstance(row, str):
            room_ids.append(row)
        if isinstance(row, tuple) and len(row) > 0:
            room_ids.append(row[0])
    return room_ids
