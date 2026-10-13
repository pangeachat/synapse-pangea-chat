"""Writing incidents from the moderation path, and retrying the ones that fail.

`IncidentStore` is the table; this is the policy around it.

**Moderation never waits on the record.** A Tier 1 block is refused before
its row is written, and a Tier 2 redaction is sent before its row is: the row
records what happened, with its final outcome, in one write. What a failed
write costs is the record, never the safety decision.

**A captured row is retried in-process, with backoff, up to five times.** The
row - not the moderation job - is what is retried: the text, the sender, the
verdict and the outcome were captured when it happened, and a retry must not
re-read any of them. The retry is in memory, so a restart before it lands
loses that one row; five failures lose it too. Both are counted
(`pangea_safety_incident_lost_total`) and logged at ERROR, with the incident
id and never the text.

**Courses resolve before the row is written, at the incident's position.**
The lookup is anchored to the stream position captured when the incident
happened (`Incident.as_of`), so resolving it late - on a retry, or at a Tier 2
verdict seconds after the message was queued - answers the same question as
resolving it at once. A lookup that fails does not stop the write: the row is
written with no courses, counted
(`pangea_safety_incident_courses_unresolved_total`) and logged, so the
incident is on record even though no course's Safety page lists it yet.
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
from synapse_pangea_chat.moderation.room_names import RoomNames

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
        room_names: Optional[RoomNames] = None,
        attempts: int = RETRY_ATTEMPTS,
        first_delay_seconds: float = RETRY_FIRST_DELAY_SECONDS,
    ) -> None:
        self._hs = homeserver
        self.store = store
        self.courses = courses
        self.room_names = room_names or RoomNames.from_homeserver(homeserver)
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
        courses = await self._courses_or_empty(incident, position)
        # Read with the courses and carried with them, so a retry does not
        # read it again. Best effort: None when it cannot be read.
        room_name = incident.room_name
        if room_name is None:
            room_name = await self.room_names.label(
                incident.room_id, incident.subject_id, position
            )
        return attr.evolve(
            incident, course_ids=courses, as_of=position, room_name=room_name
        )

    async def _courses_or_empty(
        self, incident: Incident, position: int
    ) -> Tuple[str, ...]:
        """The incident's courses, or none when the lookup fails - the row is
        written either way, because an incident with no course is still an
        incident."""
        try:
            if incident.reporter_id is not None and incident.subject_id is not None:
                return tuple(
                    await self.courses.for_report(
                        incident.subject_id, incident.reporter_id, position
                    )
                )
            if incident.subject_id is not None:
                return tuple(await self.courses.for_user(incident.subject_id, position))
            return ()
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: the row is written with no courses; counted and
            # logged by type and site.
            metrics.record_incident_courses_unresolved()
            logger.warning(
                "safety incident %s course lookup failed at %s (%s); written "
                "with no courses",
                incident.incident_id,
                error_site(exc),
                type(exc).__name__,
            )
            return ()

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
        held = [incident]

        async def _attempt() -> None:
            held[0] = await self.resolve(held[0])
            await self.store.insert(held[0])

        self.retry(kind, incident.incident_id, _attempt)

    def record_block_in_background(self, incident: Incident) -> None:
        """`record_block`, detached in a logging context of its own, so the
        refusal never waits on the write and a write that outlives the
        refused request never resumes that request's finished context."""
        try:
            run_as_background_process(
                *background_process_args(
                    self._hs, "pangea_safety_incident_block", self.record_block
                ),
                incident,
            )
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok in that nothing else can be done from here; NOT
            # silent - the row is counted lost and logged at ERROR.
            _lost("block", incident.incident_id, exc)

    # ------------------------------------------------------------------
    # Tier 2
    # ------------------------------------------------------------------

    async def record_verdict(self, incident: Incident) -> Tuple[bool, Optional[str]]:
        """Upsert a Tier 2 verdict, AFTER whatever was enforced on it, with
        its final outcome.

        Returns `(written, prior_outcome)`. A row that is not written is
        retried as captured, outcome included; nothing waits on it.
        """
        captured = [incident]
        try:
            captured[0] = await self.resolve(incident)
            prior = await self.store.upsert_verdict(captured[0])
            return True, prior
        except Exception as exc:
            reraise_if_cancelled(exc, lambda: self.retry_verdict(captured[0]))
            # silent-ok: the verdict was already enforced; the row is
            # retried.
            metrics.record_incident_write_failed("verdict")
            _log_failure(incident.incident_id, exc)
            self.retry_verdict(captured[0])
            return False, None

    def retry_verdict(self, incident: Incident) -> None:
        """Hand a captured verdict to the retry, with the outcome it carries.
        The write merges like any verdict: `removed` is final and `skipped`
        fills an empty outcome only."""

        self.retry("verdict", incident.incident_id, self._upsert_resolved(incident))

    def record_verdict_in_background(self, incident: Incident) -> None:
        """`record_verdict`, detached in a background process of its own:
        for a redaction, whose row is written after the send and must never
        hold anything up. A first try that fails is counted, logged and
        retried exactly as it is inline."""
        try:
            run_as_background_process(
                *background_process_args(
                    self._hs, "pangea_safety_incident_verdict", self.record_verdict
                ),
                incident,
            )
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok in that nothing else can be done from here; NOT
            # silent - the row is counted lost and logged at ERROR.
            _lost("verdict", incident.incident_id, exc)

    def _upsert_resolved(self, incident: Incident) -> Callable[[], Awaitable[None]]:
        """One retry's attempt. The resolved row is kept between tries, so a
        later try never looks the courses up again - a lookup that failed
        then would write the row with none."""
        held = [incident]

        async def _attempt() -> None:
            held[0] = await self.resolve(held[0])
            await self.store.upsert_verdict(held[0])

        return _attempt

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
