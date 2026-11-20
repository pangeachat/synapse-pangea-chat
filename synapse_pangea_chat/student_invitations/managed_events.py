"""The managed-account activity ledger (pangeachat/.github#799).

``pangea_managed_account_event`` is append-only, like the invitation ledger
(``events.py``): one row each time an account becomes managed by a course,
written in the same transaction as the managed record, and only when that
record is actually inserted. A row holds the course, the student, who acted
(a teacher's Matrix id, or ``system``), the action, how the student joined
the course, and the time. It is read only by a course admin of that course,
on its own route, and never appears in Seats Activity.
"""

from __future__ import annotations

from typing import Any

from synapse_pangea_chat.student_invitations.events import new_event_id

MANAGEMENT_STARTED = "management_started"
MANAGED_ACTIONS = frozenset({MANAGEMENT_STARTED})

# How the student joined: the claim's own method (``joined_via`` on the
# invitation row), or ``unknown`` for a row claimed before it was recorded.
VIA_EMAIL = "email"
VIA_GRANT = "grant"
VIA_CANVAS = "canvas"
VIA_UNKNOWN = "unknown"
VIAS = frozenset({VIA_EMAIL, VIA_GRANT, VIA_CANVAS, VIA_UNKNOWN})

MANAGED_EVENT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS pangea_managed_account_event (
        event_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        via TEXT NOT NULL,
        ts_ms BIGINT NOT NULL)""",
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
) -> str:
    """Append one event inside the caller's transaction; returns its id."""
    if action not in MANAGED_ACTIONS:
        raise ValueError("unknown managed-account event action")
    if via not in VIAS:
        raise ValueError("unknown managed-account join method")
    event_id = new_event_id()
    txn.execute(
        "INSERT INTO pangea_managed_account_event"
        " (event_id, room_id, user_id, actor, action, via, ts_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, room_id, user_id, actor, action, via, now_ms),
    )
    return event_id
