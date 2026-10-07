"""Writing incidents from the moderation path, and retrying the ones that fail.

`IncidentStore` is the table; this is the policy around it.

**A captured row is retried in-process, with backoff, up to five times.** A
Tier 1 block has already refused the send and a Tier 2 verdict has already
declined to redact, so what a failed write costs is the record, not the
safety decision. The row - not the moderation job - is what is retried: the
text, the sender and the verdict were captured when it happened, and a retry
must not re-read any of them. The retry is in memory, so a restart before it
lands loses that one row; five failures lose it too. Both are counted
(`pangea_safety_incident_lost_total`) and logged at ERROR, with the incident
id and never the text.

**Courses resolve before the row is written, at the incident's position.**
A row written with no courses reaches no Safety page while looking recorded,
so a row's courses are resolved first and a failed lookup is a failed write.
The lookup is anchored to the stream position captured when the incident
happened (`Incident.as_of`), so resolving it late - on a retry, or at a Tier 2
verdict seconds after the message was queued - answers the same question as
resolving it at once. Once resolved, the courses are carried by the retry and
never looked up again.
"""

from typing import Any, Awaitable, Callable, Optional, Tuple

import attr
from synapse.logging.context import PreserveLoggingContext, make_deferred_yieldable
from synapse.metrics.background_process_metrics import run_as_background_process
from twisted.internet import defer

from synapse_pangea_chat.moderation import metrics
from synapse_pangea_chat.moderation.compat import (
    _SecondsInterval,
    background_process_args,
    reraise_if_cancelled,
)
from synapse_pangea_chat.moderation.courses import StudentCourses
from synapse_pangea_chat.moderation.incidents import Incident, IncidentStore
from synapse_pangea_chat.moderation.log_safety import error_site, scrubbing_logger

logger = scrubbing_logger("synapse.modules.synapse_pangea_chat.moderation.recorder")

#: Retries after the first failure, and the delay before the first of them.
#: Each later delay doubles: 1, 2, 4, 8 and 16 seconds, so a database that is
#: briefly down is outlasted and one that is down for good is not hammered.
RETRY_ATTEMPTS = 5
RETRY_FIRST_DELAY_SECONDS = 1.0


class IncidentRecorder:
    """Writes incidents, and retries the ones the database refused."""

    def __init__(
        self,
        homeserver: Any,
        store: IncidentStore,
        courses: StudentCourses,
        *,
        attempts: int = RETRY_ATTEMPTS,
        first_delay_seconds: float = RETRY_FIRST_DELAY_SECONDS,
    ) -> None:
        self._hs = homeserver
        self.store = store
        self.courses = courses
        self._attempts = attempts
        self._first_delay = first_delay_seconds

    def position_now(self) -> Optional[int]:
        """The position to anchor an incident happening now, or None when it
        cannot be read - the row is then anchored when it is written."""
        try:
            return self.courses.position_now()
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: `resolve` reads the position itself when the row is
            # written; logged by type and site.
            logger.warning(
                "safety incident position unreadable at %s (%s)",
                error_site(exc),
                type(exc).__name__,
            )
            return None

    async def resolve(self, incident: Incident) -> Incident:
        """The incident with its courses, read at its own position."""
        if incident.course_ids is not None:
            return incident
        position = incident.as_of
        if position is None:
            position = self.courses.position_now()
        if incident.reporter_id is not None and incident.subject_id is not None:
            courses = await self.courses.for_report(
                incident.subject_id, incident.reporter_id, position
            )
        elif incident.subject_id is not None:
            courses = await self.courses.for_user(incident.subject_id, position)
        else:
            courses = ()
        return attr.evolve(incident, course_ids=courses, as_of=position)

    # ------------------------------------------------------------------
    # Tier 1
    # ------------------------------------------------------------------

    async def record_block(self, incident: Incident) -> bool:
        """Write a Tier 1 block. Returns whether it landed; never raises
        except to let a cancellation past, and schedules the retry either
        way, because the send is refused whatever happens here."""
        captured = [incident]
        try:
            captured[0] = await self.resolve(incident)
            await self.store.insert(captured[0])
            return True
        except Exception as exc:
            reraise_if_cancelled(exc, lambda: self._retry_insert("block", captured[0]))
            # silent-ok: the send is still refused; the row is retried.
            metrics.record_incident_write_failed("block")
            _log_failure(incident.incident_id, exc)
            self._retry_insert("block", captured[0])
            return False

    def _retry_insert(self, kind: str, incident: Incident) -> None:
        async def _attempt() -> None:
            await self.store.insert(await self.resolve(incident))

        self.retry(kind, incident.incident_id, _attempt)

    # ------------------------------------------------------------------
    # Tier 2
    # ------------------------------------------------------------------

    async def record_verdict(self, incident: Incident) -> Tuple[bool, Optional[str]]:
        """Upsert a Tier 2 verdict BEFORE any enforcement.

        Returns `(written, prior_outcome)`. When `written` is False the
        caller must not redact: the message stays visible, and the captured
        row is retried WITHOUT the outcome it carried, because no attempt
        will follow it.
        """
        captured = [incident]
        try:
            captured[0] = await self.resolve(incident)
            prior = await self.store.upsert_verdict(captured[0])
            return True, prior
        except Exception as exc:
            reraise_if_cancelled(exc, lambda: self.retry_verdict(captured[0]))
            # silent-ok: nothing is enforced on this verdict; the row is
            # retried, and the caller counts the redaction it did not send.
            metrics.record_incident_write_failed("verdict")
            _log_failure(incident.incident_id, exc)
            self.retry_verdict(captured[0])
            return False, None

    def retry_verdict(self, incident: Incident) -> None:
        """Hand a captured verdict to the retry, as a row that records no
        attempt: whoever calls this is not going to redact on it."""
        captured = attr.evolve(incident, outcome=None, attempt_id=None)

        async def _attempt() -> None:
            await self.store.upsert_verdict(await self.resolve(captured))

        self.retry("verdict", incident.incident_id, _attempt)

    def settle_outcome(
        self, incident_id: str, outcome: str, attempt_id: Optional[str]
    ) -> None:
        """Record how a redaction attempt ended, detached and retried.

        Scoped to the attempt, so a result that lands late cannot overwrite
        a later attempt's. Detached because the caller may be being cancelled
        at the drain, and an outcome lost then leaves the row `pending` until
        the next startup sweep - which is the backstop, not the plan.
        """

        async def _attempt() -> None:
            await self.store.set_outcome(incident_id, outcome, attempt_id=attempt_id)

        self.retry("outcome", incident_id, _attempt, immediately=True)

    # ------------------------------------------------------------------
    # The retry loop
    # ------------------------------------------------------------------

    def retry(
        self,
        kind: str,
        incident_id: str,
        attempt: Callable[[], Awaitable[None]],
        *,
        immediately: bool = False,
    ) -> None:
        try:
            run_as_background_process(
                *background_process_args(
                    self._hs, "pangea_safety_incident_retry", self._retry_loop
                ),
                kind,
                incident_id,
                attempt,
                immediately,
            )
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok in that nothing else can be done from here; NOT
            # silent - the row is counted lost and logged at ERROR.
            _lost(kind, incident_id, exc)

    async def _retry_loop(
        self,
        kind: str,
        incident_id: str,
        attempt: Callable[[], Awaitable[None]],
        immediately: bool,
    ) -> None:
        delay = self._first_delay
        last: Optional[BaseException] = None
        tries = self._attempts + (1 if immediately else 0)
        for index in range(tries):
            if index > 0 or not immediately:
                if not await self._sleep(delay):
                    break
                delay *= 2
            try:
                await attempt()
                return
            except Exception as exc:
                reraise_if_cancelled(exc)
                # silent-ok: the next iteration retries; the last one counts.
                last = exc
                logger.warning(
                    "safety incident %s (%s) retry failed at %s (%s)",
                    incident_id,
                    kind,
                    error_site(exc),
                    type(exc).__name__,
                )
        _lost(kind, incident_id, last)

    async def _sleep(self, seconds: float) -> bool:
        """Wait on the homeserver clock. False when the clock refuses, which
        is a process that is shutting down."""
        waiter: "defer.Deferred[None]" = defer.Deferred()

        def _fire() -> None:
            if waiter.called:
                return
            with PreserveLoggingContext():
                waiter.callback(None)

        try:
            self._hs.get_clock().call_later(_SecondsInterval(seconds), _fire)
        except Exception:
            # silent-ok: the caller counts the row lost.
            return False
        await make_deferred_yieldable(waiter)
        return True


def _log_failure(incident_id: str, exc: BaseException) -> None:
    logger.warning(
        "safety incident %s could not be written at %s (%s); it will be retried",
        incident_id,
        error_site(exc),
        type(exc).__name__,
    )


def _lost(kind: str, incident_id: str, exc: Optional[BaseException]) -> None:
    metrics.record_incident_lost(kind)
    logger.error(
        "safety incident %s (%s) was not written after every retry (%s); the "
        "Safety page will not show it",
        incident_id,
        kind,
        type(exc).__name__ if exc is not None else "no clock",
    )
