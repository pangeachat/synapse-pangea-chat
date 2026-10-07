"""What the Safety page's table needs once per boot: the backfill and the sweep.

Both run on the one instance that runs background tasks, in a background
process, after the module has loaded.

**The one-time backfill** copies `pangea_moderation_disposition` - every
Tier 2 decision taken before the Safety page existed - into
`pangea_safety_incidents`, so a course's page starts with its history rather
than empty. It runs once: a marker row in `pangea_safety_incidents_meta` is
written only after every row has been copied, so a crash midway runs it again,
and each row is inserted only where no incident exists, so a live verdict
already written is never overwritten.

- `self_harm` is true for every legacy `preserved` row. Preservation only
  ever happened for self-harm, and the stored category is the summarised one,
  which can name a different category that rode in the same verdict.
- Sender and text come from `event_json` while they are not yet pruned. A
  redacted event keeps its original JSON until Synapse's retention prunes it;
  after that the content is empty and `text` is NULL.
- `course_ids` is the course spaces where, AT THE INCIDENT EVENT'S position,
  the sender was joined below power level 100 - read from Synapse's state
  after the last event of each space at or before that position, not from
  current state. A learner who has since left still lands on the course they
  were in. This rule is used only here.
- `outcome` is `removed` when the event is redacted now, `unknown` for a
  legacy redaction whose event is still standing (a claim that was kept or
  stranded), and empty otherwise.

**The sweep** settles rows a crash left `pending`: a redaction attempt whose
result was never recorded. It re-reads the event and records `removed` when
it is redacted, `unknown` otherwise. Only rows last written before this boot
are touched, so an attempt this process starts meanwhile is not.
"""

import json
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from synapse_pangea_chat.moderation.compat import (
    _SecondsInterval,
    background_process_args,
    reraise_if_cancelled,
)
from synapse_pangea_chat.moderation.courses import (
    StateMap,
    StudentCourses,
    is_student_in,
    state_after_event_reader,
)
from synapse_pangea_chat.moderation.disposition import CREATE_TABLE_SQL
from synapse_pangea_chat.moderation.incidents import (
    ACTION_PRESERVED,
    ACTION_REDACTED,
    OUTCOME_PENDING,
    OUTCOME_REMOVED,
    OUTCOME_UNKNOWN,
    SOURCE_MODERATION,
    Incident,
    IncidentStore,
    mod_incident_id,
)
from synapse_pangea_chat.moderation.log_safety import error_site, scrubbing_logger

logger = scrubbing_logger(
    "synapse.modules.synapse_pangea_chat.moderation.safety_startup"
)

BACKFILL_MARKER = "disposition_backfill"

_SELECT_DISPOSITIONS_SQL = """
    SELECT event_id, room_id, disposition, category, decided_at_ms
    FROM pangea_moderation_disposition
    ORDER BY decided_at_ms, event_id
"""

_SELECT_EVENT_SQL = """
    SELECT e.sender, e.stream_ordering, e.type, j.json
    FROM events AS e
    LEFT JOIN event_json AS j ON j.event_id = e.event_id
    WHERE e.event_id = ?
"""

_SELECT_CANDIDATE_ROOMS_SQL = """
    SELECT DISTINCT room_id FROM room_memberships WHERE user_id = ?
"""

_SELECT_LAST_EVENT_AT_SQL = """
    SELECT event_id FROM events
    WHERE room_id = ? AND stream_ordering <= ? AND outlier = ?
    ORDER BY stream_ordering DESC
    LIMIT 1
"""

#: For the drift test.
STATEMENTS = (
    _SELECT_DISPOSITIONS_SQL,
    _SELECT_EVENT_SQL,
    _SELECT_CANDIDATE_ROOMS_SQL,
    _SELECT_LAST_EVENT_AT_SQL,
)

StateAfter = Callable[[str, str], Awaitable[StateMap]]


class SafetyIncidentsStartup:
    def __init__(
        self,
        homeserver: Any,
        store: IncidentStore,
        courses: StudentCourses,
        *,
        state_after: Optional[StateAfter] = None,
    ) -> None:
        self._hs = homeserver
        self._store = store
        self._courses = courses
        self._state_after = state_after or state_after_event_reader(homeserver)
        self._booted_ms = int(homeserver.get_clock().time_msec())

    def schedule(self) -> None:
        """Run once, on the next reactor turn."""
        from synapse.metrics.background_process_metrics import (
            run_as_background_process,
        )

        def _start() -> None:
            run_as_background_process(
                *background_process_args(self._hs, "pangea_safety_startup", self.run)
            )

        self._hs.get_clock().call_later(_SecondsInterval(0), _start)

    async def run(self) -> None:
        """The backfill, then the sweep. Each fails on its own: a backfill
        that cannot finish does not stop pending rows being settled."""
        try:
            await self.backfill()
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: retried on the next boot, because the marker is
            # written only when the backfill completes.
            logger.error(
                "safety incident backfill did not complete at %s (%s); it "
                "runs again on the next start",
                error_site(exc),
                type(exc).__name__,
            )
        try:
            await self.sweep()
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: the rows stay pending and the next start sweeps them.
            logger.error(
                "safety incident sweep did not complete at %s (%s)",
                error_site(exc),
                type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # The sweep
    # ------------------------------------------------------------------

    async def sweep(self) -> int:
        """Settle every row left `pending` before this boot. Returns how many
        were settled."""
        settled = 0
        for incident_id, event_id in await self._store.pending_before(self._booted_ms):
            outcome = OUTCOME_UNKNOWN
            if event_id is not None and await self._is_redacted(event_id) is True:
                outcome = OUTCOME_REMOVED
            if await self._store.set_outcome(
                incident_id,
                outcome,
                only_if=OUTCOME_PENDING,
                updated_before_ms=self._booted_ms,
            ):
                settled += 1
        if settled:
            logger.info("safety incident sweep settled %d pending rows", settled)
        return settled

    async def _is_redacted(self, event_id: str) -> Optional[bool]:
        event = await self._hs.get_datastores().main.get_event(
            event_id, allow_none=True
        )
        if event is None:
            return None
        return bool(event.internal_metadata.is_redacted())

    # ------------------------------------------------------------------
    # The backfill
    # ------------------------------------------------------------------

    async def backfill(self) -> int:
        """Copy the legacy dispositions, once. Returns how many rows it
        wrote."""
        if await self._store.is_done(BACKFILL_MARKER):
            return 0
        pool = self._hs.get_datastores().main.db_pool

        def _read(txn: Any) -> List[Tuple[Any, ...]]:
            # Created if absent, so an instance that never ran Tier 2 reads
            # an empty table rather than failing every boot.
            txn.execute(CREATE_TABLE_SQL)
            txn.execute(_SELECT_DISPOSITIONS_SQL)
            return list(txn.fetchall())

        rows = await pool.runInteraction("pangea_safety_backfill_read", _read)
        written = 0
        failed = 0
        for event_id, room_id, disposition, category, decided_at_ms in rows:
            # One row at a time, each on its own: an event whose state cannot
            # be read must not cost every other row its copy.
            try:
                incident = await self._legacy_incident(
                    event_id, room_id, disposition, category, int(decided_at_ms)
                )
                stored = await self._store.insert(incident)
            except Exception as exc:
                reraise_if_cancelled(exc)
                # silent-ok: counted, logged at ERROR, and the marker is not
                # written, so the whole backfill - idempotent - runs again on
                # the next start.
                failed += 1
                logger.error(
                    "safety incident backfill could not copy the decision on "
                    "%s at %s (%s)",
                    event_id,
                    error_site(exc),
                    type(exc).__name__,
                )
                continue
            if stored is incident:
                written += 1
        if failed == 0:
            await self._store.mark_done(BACKFILL_MARKER)
        logger.info(
            "safety incident backfill copied %d of %d legacy decisions, %d failed",
            written,
            len(rows),
            failed,
        )
        return written

    async def _legacy_incident(
        self,
        event_id: str,
        room_id: str,
        disposition: str,
        category: str,
        decided_at_ms: int,
    ) -> Incident:
        from synapse_pangea_chat.moderation import extract_message_text

        pool = self._hs.get_datastores().main.db_pool

        def _event(txn: Any) -> Optional[Tuple[Any, ...]]:
            txn.execute(_SELECT_EVENT_SQL, (event_id,))
            row = txn.fetchone()
            return None if row is None else tuple(row)

        row = await pool.runInteraction("pangea_safety_backfill_event", _event)
        subject_id: Optional[str] = None
        text: Optional[str] = None
        courses: Tuple[str, ...] = ()
        if row is not None:
            sender, stream_ordering, event_type, raw_json = row
            subject_id = sender
            if raw_json is not None:
                parsed = json.loads(raw_json)
                content = parsed.get("content") if isinstance(parsed, dict) else None
                text = extract_message_text(event_type, content, event_id).text
            courses = await self._courses_at(sender, int(stream_ordering))
        redacted = await self._is_redacted(event_id)
        preserved = disposition == ACTION_PRESERVED
        if redacted is True:
            outcome: Optional[str] = OUTCOME_REMOVED
        elif disposition == ACTION_REDACTED:
            outcome = OUTCOME_UNKNOWN
        else:
            outcome = None
        now_ms = self._store.now_ms()
        return Incident(
            incident_id=mod_incident_id(event_id),
            source=SOURCE_MODERATION,
            action=ACTION_PRESERVED if preserved else ACTION_REDACTED,
            outcome=outcome,
            subject_id=subject_id,
            reporter_id=None,
            room_id=room_id,
            event_id=event_id,
            course_ids=courses,
            categories=(category,),
            # Preservation only ever happened for self-harm; the stored
            # category is a summary and can name another category.
            self_harm=preserved or category == "self_harm",
            rule=None,
            top_score=None,
            text=text,
            reason=None,
            created_ms=decided_at_ms,
            updated_ms=max(now_ms, decided_at_ms),
        )

    async def _courses_at(self, user_id: str, position: int) -> Tuple[str, ...]:
        """The user's student courses at a stream position, from the state of
        each space after its last event at or before that position."""
        if self._courses.excluded(user_id):
            return ()
        pool = self._hs.get_datastores().main.db_pool

        def _candidates(txn: Any) -> List[Tuple[str, Optional[str]]]:
            txn.execute(_SELECT_CANDIDATE_ROOMS_SQL, (user_id,))
            rooms = sorted(row[0] for row in txn.fetchall())
            result: List[Tuple[str, Optional[str]]] = []
            for room_id in rooms:
                txn.execute(_SELECT_LAST_EVENT_AT_SQL, (room_id, position, False))
                last = txn.fetchone()
                result.append((room_id, None if last is None else last[0]))
            return result

        candidates = await pool.runInteraction(
            "pangea_safety_backfill_candidates", _candidates
        )
        courses: List[str] = []
        for room_id, last_event_id in candidates:
            if last_event_id is None:
                # The room did not exist yet at that position.
                continue
            state = await self._state_after(last_event_id, user_id)
            if is_student_in(state, user_id):
                courses.append(room_id)
        return tuple(courses)
