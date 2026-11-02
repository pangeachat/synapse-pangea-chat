"""The email of an invitation set for a later time.

A looping call on one process sends what has come due, a small batch a tick.
It needs no teacher's token: the teacher's call set the time, and each send
re-reads the row, so a row revoked or claimed since is never emailed.

The claim counts the send and takes the row out of the due set before any
email, so a process dying mid-send loses that row's email rather than sending
it twice: a resend is explicit. A failed send puts it back or gives it up:

- the mail service failed: the row stays due and the tick stops, as the send
  route stops, so an outage heals by itself, one attempt a minute. A row still
  failing past ``RETRY_WINDOW_MS`` after its time is given up.
- the mail server refused the address: given up, it would only fail again.
- the course has no class code: none of its rows can be sent, so all are given
  up, with one report.

A row given up stays on the roster, unscheduled, for the teacher to send.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, cast

from synapse.metrics.background_process_metrics import run_as_background_process

from synapse_pangea_chat.moderation.compat import (
    background_process_args,
    looping_call_interval,
)
from synapse_pangea_chat.push_rules_writer import writes_push_rules
from synapse_pangea_chat.student_invitations.api import (
    MAX_SEND_AHEAD_MS,
    CourseRooms,
    Mailer,
    send_failure,
)
from synapse_pangea_chat.student_invitations.claim import now_ms
from synapse_pangea_chat.student_invitations.events import SYSTEM
from synapse_pangea_chat.student_invitations.report import report_failure
from synapse_pangea_chat.student_invitations.store import StudentInvitationStore

__all__ = [
    "BATCH",
    "MAX_SEND_AHEAD_MS",
    "RETRY_WINDOW_MS",
    "TICK_SECONDS",
    "ScheduledInvites",
]

TICK_SECONDS = 60
BATCH = 20
RETRY_WINDOW_MS = 3 * 24 * 60 * 60 * 1000


class NoClassCode(Exception):
    """A course's invitations can no longer be sent: nothing to link to."""


class ScheduledInvites:
    def __init__(
        self,
        api: Any,
        store: StudentInvitationStore,
        rooms: CourseRooms,
        mailer: Mailer,
        now: Callable[[], int] = now_ms,
    ) -> None:
        self._api = api
        self._store = store
        self._rooms = rooms
        self._mailer = mailer
        self._now = now
        self._running = False

    def start(self) -> None:
        # One process sends, as the notice schedule does: the push rules
        # writer is the instance every deployment has exactly one of. The
        # claim alone keeps a row to one email; this keeps one loop.
        if not writes_push_rules(self._api):
            return
        homeserver = self._api._hs
        homeserver.get_clock().looping_call(
            cast(Any, run_as_background_process),
            cast(Any, looping_call_interval(TICK_SECONDS)),
            *background_process_args(
                homeserver, "pangea_scheduled_invites", self.run_due
            ),
        )

    async def run_due(self) -> None:
        # The timer does not await the previous tick: a slow mail server must
        # not grow concurrent senders.
        if self._running:
            return
        self._running = True
        try:
            for _ in range(BATCH):
                row = await self._store.next_due(self._now())
                if row is None or not await self._deliver(row):
                    return
        except Exception as error:
            report_failure("scheduled invitations tick", error)
        finally:
            self._running = False

    async def _deliver(self, row: Dict[str, Any]) -> bool:
        """Email one due row. False when the tick should stop."""
        room_id = row["course_room_id"]
        ident = row["id"]
        # Read before the claim, so the claim is the last check of the row
        # before the email. A failed read claims nothing: the row stays due.
        code = await self._rooms.access_code(room_id)
        if not code:
            count = await self._store.unschedule_course(room_id)
            report_failure(
                "scheduled invitations", NoClassCode(), room=room_id, count=count
            )
            return True
        if not await self._store.claim_due(row, self._now()):
            return True
        try:
            await self._mailer.send_invite(row, code)
        except Exception as error:
            reason = send_failure(error)
            late = self._now() - row["send_at_ms"] > RETRY_WINDOW_MS
            retry = reason == "mail_error" and not late
            await self._store.release_send(row, keep_schedule=retry)
            report_failure(
                "scheduled invitation",
                error,
                invitation=ident,
                reason=reason,
                retry=retry,
            )
            return reason != "mail_error"
        try:
            await self._store.record_sent(row, SYSTEM, self._now())
        except Exception as error:
            # The email went out and its count stands; only the ledger row is
            # missing, as in the send route.
            report_failure("scheduled invite sent event", error, invitation=ident)
        return True
