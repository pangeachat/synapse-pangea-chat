"""The managed-account record (SPEC §6, CONTROLS-SPEC.md F2/F4).

One row per (account, course), written by every claim together with the
invitation's move to ``joined`` (``StudentInvitationStore.claim_txn``), and
deleted when that invitation is revoked (``revoke``) or its claimant leaves,
is kicked or is banned from the course (``release_on_leave``). v1 records it
only; no control reads it yet.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from synapse_pangea_chat.student_invitations.store import StudentInvitationStore


async def managed_record(
    store: StudentInvitationStore, user_id: str, room_id: str
) -> Optional[Dict[str, Any]]:
    """The record naming who manages ``user_id`` in ``room_id``, if any."""
    return await store.managed_record(user_id, room_id)
