from typing import Any, List

from synapse_pangea_chat.room_code.code_index import RoomCodeMatch, code_index_for

__all__ = ["RoomCodeMatch", "get_rooms_with_access_code"]


async def get_rooms_with_access_code(
    access_code: str, room_store: Any
) -> List[RoomCodeMatch]:
    """Rooms whose current join rules carry the code as `access_code` or
    `admin_access_code`, ignoring case."""
    return await code_index_for(room_store).lookup(access_code)
