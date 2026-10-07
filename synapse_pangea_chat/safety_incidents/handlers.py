"""The two Safety page endpoints, as plain async functions over their inputs.

`resources.py` puts them on HTTP. Kept apart so every admission rule is tested
against a homeserver double without a request object in the way.

**Report** (`POST /_synapse/client/pangea/v1/report`, reporter's token).
Admission fails closed, in this order: the event exists (404), it belongs to
`room_id` (403), and it is visible to the caller (403) - the last one asked of
Synapse's own event handler, which binds the event to the room and applies
membership-aware history visibility, rather than of the visibility filter
directly. The row snapshots the sender and the content as the caller can see
it now; an event already redacted is still recorded, with no text.
`report:<report_id>` is idempotent: a retry with the same id is answered from
the first write.

**Read** (`GET /_synapse/client/pangea/v1/safety_incidents?space_id=`, a
course admin's token). Fails closed unless `space_id` is a course space, the
caller is currently joined, and their power level there is 100 or above.
Every refusal is the same 403 with the same body, so the endpoint is no oracle
for which rooms exist or which checks a caller failed.

Neither logs a word of a message or a reason.
"""

import re
from typing import Any, Dict, Optional, Tuple

from synapse.api.errors import AuthError
from synapse.types import RoomID, UserID

from synapse_pangea_chat.moderation import extract_message_text
from synapse_pangea_chat.moderation.courses import (
    StudentCourses,
    current_state_reader,
    is_course_admin_in,
)
from synapse_pangea_chat.moderation.incidents import (
    ACTION_REPORTED,
    SOURCE_REPORT,
    Incident,
    IncidentStore,
    report_incident_id,
)
from synapse_pangea_chat.moderation.room_names import RoomNames

JOIN = "join"

#: A client-generated uuid in practice. Bounded and plain, because it becomes
#: part of a primary key and of every log line about the report.
_REPORT_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")

FORBIDDEN_READ: Dict[str, Any] = {
    "error": "Forbidden: course admin required",
    "errcode": "M_FORBIDDEN",
}

Response = Tuple[int, Dict[str, Any]]


def _bad_request(message: str) -> Response:
    return 400, {"error": message, "errcode": "M_BAD_JSON"}


def _valid_room_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        RoomID.from_string(value)
    # silent-ok: validation only - the caller answers 400 for an invalid id
    except Exception:
        return False
    return True


def _valid_event_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("$") and 1 < len(value) <= 255


class ReportHandler:
    def __init__(
        self,
        homeserver: Any,
        store: IncidentStore,
        courses: StudentCourses,
        room_names: Optional[RoomNames] = None,
    ) -> None:
        self._hs = homeserver
        self._store = store
        self._courses = courses
        self._room_names = room_names or RoomNames.from_homeserver(homeserver)

    async def report(self, reporter: UserID, body: Any) -> Response:
        if not isinstance(body, dict):
            return _bad_request("Request body must be a JSON object")
        report_id = body.get("report_id")
        room_id = body.get("room_id")
        event_id = body.get("event_id")
        reason = body.get("reason")
        if not isinstance(report_id, str) or not _REPORT_ID.match(report_id):
            return _bad_request("'report_id' must be a client-generated id")
        if not _valid_room_id(room_id):
            return _bad_request("'room_id' must be a valid Matrix room ID")
        if not _valid_event_id(event_id):
            return _bad_request("'event_id' must be a valid Matrix event ID")
        if reason is not None and not isinstance(reason, str):
            return _bad_request("'reason' must be a string")

        # 1. The event exists.
        stored = await self._hs.get_datastores().main.get_event(
            event_id, allow_none=True
        )
        if stored is None:
            return 404, {"error": "Event not found", "errcode": "M_NOT_FOUND"}
        # 2. It belongs to the room the caller named.
        if stored.room_id != room_id:
            return 403, {
                "error": "Event is not in that room",
                "errcode": "M_FORBIDDEN",
            }
        # 3. The caller can see it. Synapse's own handler, which checks the
        # room binding again and applies membership-aware visibility.
        try:
            visible = await self._hs.get_event_handler().get_event(
                reporter, room_id, event_id
            )
        except AuthError:
            visible = None
        if visible is None:
            return 403, {
                "error": "You cannot see that event",
                "errcode": "M_FORBIDDEN",
            }
        event = getattr(visible, "event", visible)

        reporter_id = reporter.to_string()
        subject_id = event.sender
        text: Optional[str] = None
        if not event.internal_metadata.is_redacted():
            text = extract_message_text(event.type, event.content, event.event_id).text
        # At report time: there is no send-time snapshot for a message that
        # was never flagged.
        position = self._courses.position_now()
        courses = await self._courses.for_report(subject_id, reporter_id, position)
        room_name = await self._room_names.label(room_id, subject_id, position)
        now_ms = self._store.now_ms()
        incident_id = report_incident_id(report_id)
        written = await self._store.insert(
            Incident(
                incident_id=incident_id,
                source=SOURCE_REPORT,
                action=ACTION_REPORTED,
                outcome=None,
                subject_id=subject_id,
                reporter_id=reporter_id,
                room_id=room_id,
                event_id=event_id,
                room_name=room_name,
                course_ids=courses,
                categories=(),
                self_harm=False,
                rule=None,
                top_score=None,
                text=text,
                reason=reason,
                created_ms=now_ms,
                updated_ms=now_ms,
            )
        )
        if written.reporter_id != reporter_id or written.event_id != event_id:
            # The id is already a different report. A retry is the same
            # reporter about the same event; anything else is a collision,
            # and answering 200 would tell this reporter their report was
            # recorded when it was not.
            return 409, {
                "error": "report_id is already in use",
                "errcode": "M_UNKNOWN",
            }
        return 200, {"incident_id": incident_id}


class ReadHandler:
    def __init__(self, homeserver: Any, store: IncidentStore) -> None:
        self._hs = homeserver
        self._store = store
        self._read_state = current_state_reader(homeserver)

    async def read(self, caller_id: str, space_id: Any) -> Response:
        if not _valid_room_id(space_id):
            return 400, {
                "error": "'space_id' must be a valid Matrix room ID",
                "errcode": "M_INVALID_PARAM",
            }
        if not await self.is_course_admin(caller_id, space_id):
            return 403, dict(FORBIDDEN_READ)
        incidents = await self._store.for_course(space_id)
        return 200, {"incidents": [incident.to_json() for incident in incidents]}

    async def is_course_admin(self, caller_id: str, space_id: str) -> bool:
        """Currently joined, at power level 100 or above, in a course space.
        Membership first, from the local membership table, so a caller who
        has left is refused before anything about the room is read."""
        store = self._hs.get_datastores().main
        membership, _ = await store.get_local_current_membership_for_user_in_room(
            caller_id, space_id
        )
        if membership != JOIN:
            return False
        state = await self._read_state(space_id, caller_id)
        return is_course_admin_in(state, caller_id)
