"""The invitation activity ledger (seats amendment 2026-10-10, §2).

``pangea_student_invitation_event`` is append-only: one row per invitation
action, written in the same transaction as the change it records, by the
store that makes that change. A row holds the course, the time, who acted (a
teacher's Matrix id, or ``system`` for what happens on its own: a claim, a
request, a leave), the action, the invitation it concerns, and a small count
where one applies. It never holds an email. The actor is returned only to a
course admin's read (T12) and is never logged.
"""

from __future__ import annotations

import itertools
import secrets
from typing import Any, Optional

SYSTEM = "system"

# Event ids sort in write order within a process (a counter, then randomness
# for uniqueness across processes), so the events of one transaction, which
# share a millisecond, read back in the order they happened.
_SEQUENCE = itertools.count()

STUDENTS_ADDED = "students_added"
INVITE_SENT = "invite_sent"
INVITE_RESENT = "invite_resent"
INVITATION_WITHDRAWN = "invitation_withdrawn"
REQUEST_MADE = "request_made"
REQUEST_GRANTED = "request_granted"
REQUEST_DENIED = "request_denied"
INVITATION_CLAIMED = "invitation_claimed"
STUDENT_LEFT = "student_left"
CANVAS_CONNECTED = "canvas_connected"
CANVAS_ROSTER_IMPORTED = "canvas_roster_imported"

ACTIONS = frozenset(
    {
        STUDENTS_ADDED,
        INVITE_SENT,
        INVITE_RESENT,
        INVITATION_WITHDRAWN,
        REQUEST_MADE,
        REQUEST_GRANTED,
        REQUEST_DENIED,
        INVITATION_CLAIMED,
        STUDENT_LEFT,
        CANVAS_CONNECTED,
        CANVAS_ROSTER_IMPORTED,
    }
)

EVENT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS pangea_student_invitation_event (
        event_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL,
        ts_ms BIGINT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        invitation_id TEXT,
        count INTEGER)""",
    """CREATE INDEX IF NOT EXISTS pangea_student_invitation_event_room
        ON pangea_student_invitation_event (room_id, ts_ms, event_id)""",
)


def record_event(
    txn: Any,
    *,
    room_id: str,
    actor: str,
    action: str,
    now_ms: int,
    invitation_id: Optional[str] = None,
    count: Optional[int] = None,
) -> str:
    """Append one event inside the caller's transaction; returns its id."""
    if action not in ACTIONS:
        raise ValueError("unknown invitation event action")
    event_id = f"{next(_SEQUENCE):012d}{secrets.token_hex(8)}"
    txn.execute(
        "INSERT INTO pangea_student_invitation_event"
        " (event_id, room_id, ts_ms, actor, action, invitation_id, count)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, room_id, now_ms, actor, action, invitation_id, count),
    )
    return event_id
