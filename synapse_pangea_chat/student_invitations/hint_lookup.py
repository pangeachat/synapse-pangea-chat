"""The public invitation hint and the managed-account disclosure.

``GET .../student_invitations/hint?invitation_id=`` answers, for an ``invited``
invitation only, the course name and a masked hint of the invited address
(``<first char>***@<domain>``) for the sign-up screen. Unknown, malformed,
revoked, joined and left ids all get the same 404, so the route says nothing
about an id beyond "you may sign up for this". The full address, the room id
and the state are never in the answer (INV-8).
"""

from __future__ import annotations

from typing import Any, Optional

from synapse_pangea_chat.student_invitations.store import STATE_INVITED

# Ids are token_urlsafe(16) (22 characters); anything far longer is not one.
MAX_INVITATION_ID_LENGTH = 64


def mask_email(address: str) -> str:
    local, _, domain = address.strip().lower().rpartition("@")
    return f"{local[:1]}***@{domain}"


def plausible_id(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    if len(value) > MAX_INVITATION_ID_LENGTH:
        return None
    return value


def hintable(row: Optional[dict]) -> bool:
    return row is not None and row["state"] == STATE_INVITED
