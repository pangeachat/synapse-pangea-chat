"""Code lookups that see every place a code can live.

Class codes and client-set admin codes live in join rules. A requested course's
claim code lives only in its claim record, because members can read join rules
(knock-with-code.instructions.md, "Claiming a course"). Anything that resolves
a code, or checks one is free, goes through here so it cannot miss the second
place.
"""

from __future__ import annotations

from typing import Any, List, Optional, Set, Tuple

from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.room_code.generate_room_code import generate_access_code
from synapse_pangea_chat.room_code.get_rooms_with_access_code import (
    RoomCodeMatch,
    get_rooms_with_access_code,
)


async def rooms_for_code(
    access_code: str, room_store: Any, claim_store: CourseClaimStore
) -> Optional[Tuple[List[RoomCodeMatch], Set[str]]]:
    """Every room the code opens, and which of them it opens as a claim.

    None when the join-rules lookup failed, as ``get_rooms_with_access_code``
    reports it.
    """
    matches = await get_rooms_with_access_code(
        access_code=access_code, room_store=room_store
    )
    if matches is None:
        return None
    claim_room_ids = set(await claim_store.rooms_for_admin_code(access_code))
    state_room_ids = {match.room_id for match in matches}
    merged = list(matches) + [
        RoomCodeMatch(room_id=room_id, is_admin_code=True)
        for room_id in sorted(claim_room_ids - state_room_ids)
    ]
    return merged, claim_room_ids


async def code_is_taken(
    access_code: str, room_store: Any, claim_store: CourseClaimStore
) -> bool:
    """Whether a newly generated code would collide with any code in use,
    including spent claim codes."""
    matches = await get_rooms_with_access_code(
        access_code=access_code, room_store=room_store
    )
    if matches is None or len(matches) > 0:
        return True
    return await claim_store.code_in_use(access_code)


#: Attempts before giving up on finding a free code.
MAX_CODE_ATTEMPTS = 10


async def new_unique_code(
    room_store: Any, claim_store: CourseClaimStore
) -> Optional[str]:
    """A freshly generated code nobody holds, or None after
    ``MAX_CODE_ATTEMPTS`` collisions."""
    for _ in range(MAX_CODE_ATTEMPTS):
        code = generate_access_code()
        if not await code_is_taken(code, room_store, claim_store):
            return code
    return None
