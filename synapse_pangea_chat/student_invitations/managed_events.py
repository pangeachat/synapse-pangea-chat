"""The managed-account activity ledger (pangeachat/.github#799).

``pangea_managed_account_event`` is append-only, like the invitation ledger
(``events.py``): a row each time an account becomes managed by a course
(written in the same transaction as the managed record, and only when that
record actually starts), is asked or declines, has a control changed, or
leaves, is removed or banned, rejoins, graduates or is moved. A row holds the course, the student,
who acted (a Matrix id, or ``system``), the action, how the student joined
the course, the time, and a ``detail`` for a control change. It is read only by a course admin of that course,
on its own route, and never appears in Seats Activity.
"""

from __future__ import annotations

from typing import Any, Optional

from synapse_pangea_chat.joined_via import VIA_CLAIM, VIA_CODE, VIA_INSTRUCTOR
from synapse_pangea_chat.student_invitations.events import new_event_id

MANAGEMENT_STARTED = "management_started"
# A Protected course's existing member is asked; declining keeps them in the
# course, unmanaged (protected.py).
MANAGEMENT_ASKED = "management_asked"
MANAGEMENT_DECLINED = "management_declined"
# One row per managed student for each control a course admin changes;
# ``detail`` names it, e.g. ``outside.dms=on``.
CONTROL_CHANGED = "control_changed"
# A managed student's membership: leaving keeps management (#799).
LEFT = "left"
REMOVED = "removed"
BANNED = "banned"
REJOINED = "rejoined"
# Management ends: a course admin graduates the student, or the team moves
# it to another course (``detail`` names it, ``to <room>`` or ``from <room>``).
GRADUATION = "graduated"
MOVED = "moved"
MANAGED_ACTIONS = frozenset(
    {
        MANAGEMENT_STARTED,
        MANAGEMENT_ASKED,
        MANAGEMENT_DECLINED,
        CONTROL_CHANGED,
        LEFT,
        REMOVED,
        BANNED,
        REJOINED,
        GRADUATION,
        MOVED,
    }
)

# How the student joined: the claim's own method (``joined_via`` on the
# invitation row), the module path that named the join (``joined_via.py``),
# an approved request or another invitation, or ``unknown``.
VIA_EMAIL = "email"
VIA_GRANT = "grant"
VIA_CANVAS = "canvas"
VIA_REQUEST = "request"
VIA_INVITE = "invite"
VIA_UNKNOWN = "unknown"
VIAS = frozenset(
    {
        VIA_EMAIL,
        VIA_GRANT,
        VIA_CANVAS,
        VIA_REQUEST,
        VIA_INVITE,
        VIA_UNKNOWN,
        VIA_CODE,
        VIA_CLAIM,
        VIA_INSTRUCTOR,
    }
)

MANAGED_EVENT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS pangea_managed_account_event (
        event_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        via TEXT NOT NULL,
        ts_ms BIGINT NOT NULL,
        detail TEXT)""",
    """CREATE INDEX IF NOT EXISTS pangea_managed_account_event_room
        ON pangea_managed_account_event (room_id, ts_ms, event_id)""",
)


def record_managed_event(
    txn: Any,
    *,
    room_id: str,
    user_id: str,
    actor: str,
    action: str,
    via: str,
    now_ms: int,
    detail: Optional[str] = None,
) -> str:
    """Append one event inside the caller's transaction; returns its id."""
    if action not in MANAGED_ACTIONS:
        raise ValueError("unknown managed-account event action")
    if via not in VIAS:
        raise ValueError("unknown managed-account join method")
    event_id = new_event_id()
    txn.execute(
        "INSERT INTO pangea_managed_account_event"
        " (event_id, room_id, user_id, actor, action, via, ts_ms, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, room_id, user_id, actor, action, via, now_ms, detail),
    )
    return event_id
