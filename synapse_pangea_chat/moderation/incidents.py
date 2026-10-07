"""The durable record of every safety incident, for the course's Safety page.

One row per incident in `pangea_safety_incidents`: a Tier 1 block, a Tier 2
verdict (removed, preserved, or flagged below its threshold and left up), or a
report a learner filed in the app. Course admins read the rows for their
course through `GET /_synapse/client/pangea/v1/safety_incidents`
(pangeachat/admin-dash#105).

Two principles, and every rule below follows from one of them:

- **Snapshot at the moment of the incident.** The sender, the full text and
  the courses the incident belongs to are captured once, when it happens, and
  never recomputed from Synapse. A learner leaving a course or a redaction
  being pruned cannot erase the evidence.
- **Never enforce without a record.** Tier 2 writes its row before it redacts
  anything, and when the write fails it does not redact.

**Text and reasons are stored and never logged.** The table is read by course
admins behind an authorisation check; a log line is read by anybody with log
access, for months. Every log line here names the incident id, the room and
the site of a failure, never a word of the message or the reporter's reason.

**Course association is its own table.** `course_ids` on the incident row is
the record; `pangea_safety_incident_courses` is its index, written in the same
transaction, so the read endpoint answers "every incident of this course"
with an indexed join rather than a scan of a JSON column. Course ids are
frozen at the first write and never change, so the two cannot drift.

**Placeholders are `?`**, for the reason in `moderation.disposition`: only
Synapse's Postgres engine rewrites them, and `%s` would fail on SQLite.

**The merge is computed in Python inside one transaction**, not in an
`ON CONFLICT DO UPDATE` expression. The rules - categories unioned, the score
kept at its maximum, the action moving only up, the outcome changing only on
an actual attempt - are easier to read, test and get right as a function, and
the read-modify-write is safe under Synapse's isolation: Postgres runs it at
REPEATABLE READ, so a concurrent writer to the same row fails with a
serialisation error that `runInteraction` retries, and SQLite has one writer.
"""

import json
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import attr

INCIDENTS_TABLE = "pangea_safety_incidents"
INCIDENT_COURSES_TABLE = "pangea_safety_incident_courses"
INCIDENTS_META_TABLE = "pangea_safety_incidents_meta"

SOURCE_MODERATION = "moderation"
SOURCE_REPORT = "report"

ACTION_BLOCKED = "blocked"
ACTION_REDACTED = "redacted"
ACTION_PRESERVED = "preserved"
ACTION_KEPT = "kept"
ACTION_REPORTED = "reported"

#: Whether a redaction was attempted and how it ended, independent of the
#: action. NULL (None here) means never attempted.
OUTCOME_PENDING = "pending"
OUTCOME_REMOVED = "removed"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_UNKNOWN = "unknown"

#: The outcomes an actual attempt produces. `skipped` is not one of them: it
#: says a redaction was decided and then not sent, so it fills an empty
#: outcome and never overwrites what an earlier attempt established.
_ATTEMPT_OUTCOMES = frozenset(
    {OUTCOME_PENDING, OUTCOME_REMOVED, OUTCOME_FAILED, OUTCOME_UNKNOWN}
)

#: A Tier 2 action only ever moves UP this order. A later, milder verdict on
#: the same message cannot make it look less serious than it was, and a
#: preserve - a self-harm disclosure - outranks everything.
_ACTION_RANK = {ACTION_KEPT: 0, ACTION_REDACTED: 1, ACTION_PRESERVED: 2}

#: U+0000 cannot be stored in a PostgreSQL text column, so it is replaced with
#: U+2400 SYMBOL FOR NULL, which a reader sees as a visible marker. Everything
#: else is stored verbatim.
_NUL = "\x00"
_NUL_SYMBOL = "␀"


def nul_safe(value: Optional[str]) -> Optional[str]:
    """The text as stored: U+0000 replaced, nothing else touched."""
    if value is None:
        return None
    return value.replace(_NUL, _NUL_SYMBOL)


def mod_incident_id(event_id: str) -> str:
    return f"mod:{event_id}"


def report_incident_id(report_id: str) -> str:
    return f"report:{report_id}"


@attr.s(auto_attribs=True, frozen=True, slots=True)
class Incident:
    """One row of `pangea_safety_incidents`.

    `course_ids` is None only in memory, for a row whose course lookup failed
    and is still to be retried; a row is never written without it.
    """

    incident_id: str
    source: str
    action: str
    outcome: Optional[str]
    subject_id: Optional[str]
    reporter_id: Optional[str]
    room_id: str
    event_id: Optional[str]
    course_ids: Optional[Tuple[str, ...]]
    categories: Tuple[str, ...]
    self_harm: bool
    rule: Optional[str]
    top_score: Optional[float]
    text: Optional[str]
    reason: Optional[str]
    created_ms: int
    updated_ms: int
    #: The room's readable name when the incident happened; see
    #: `moderation.room_names`. Same U+0000 rule as `text`.
    room_name: Optional[str] = None
    #: Which redaction attempt owns `outcome`. Internal: it is what stops a
    #: delayed result from one attempt overwriting a later attempt's. Minted
    #: by `new_attempt_id`, so it also says which process started it.
    attempt_id: Optional[str] = None
    #: What the row said before the current attempt began, so an attempt that
    #: is then not sent - declined, or abandoned at a cancellation - puts it
    #: back, ownership included. Internal.
    prior_outcome: Optional[str] = None
    prior_attempt_id: Optional[str] = None
    #: The stream position the incident's courses are read at, when they are
    #: still to be resolved. In memory only; never stored.
    as_of: Optional[int] = attr.ib(default=None, eq=False)

    def to_json(self) -> Dict[str, Any]:
        """The wire shape of the read endpoint's `Incident`.

        `course_ids` is deliberately absent: it names every course the
        subject belongs to, and a course admin reading one course's page has
        no business learning which others a learner is in.
        """
        return {
            "incident_id": self.incident_id,
            "source": self.source,
            "action": self.action,
            "outcome": self.outcome,
            "subject_id": self.subject_id,
            "reporter_id": self.reporter_id,
            "room_id": self.room_id,
            "room_name": self.room_name,
            "event_id": self.event_id,
            "categories": list(self.categories),
            "self_harm": self.self_harm,
            "rule": self.rule,
            "top_score": self.top_score,
            "text": self.text,
            "reason": self.reason,
            "created_ms": self.created_ms,
            "updated_ms": self.updated_ms,
        }


#: This process's mark on the attempts it starts. The startup sweep settles a
#: `pending` row only when another process started its attempt: an attempt
#: this process started is live, and its own result is on its way.
BOOT_TOKEN = uuid.uuid4().hex


def new_attempt_id() -> str:
    return f"{BOOT_TOKEN}:{uuid.uuid4().hex}"


def started_by_this_process(attempt_id: Optional[str]) -> bool:
    return attempt_id is not None and attempt_id.startswith(f"{BOOT_TOKEN}:")


def merge_outcome(existing: Optional[str], new: Optional[str]) -> Optional[str]:
    """The outcome after a write that carries `new`.

    - `None` is "this write made no attempt", and changes nothing.
    - `removed` is final: the message is gone and nothing can say otherwise.
    - An actual attempt's result replaces whatever an earlier attempt left.
    - `skipped` fills an empty outcome only.
    """
    if new is None:
        return existing
    if existing == OUTCOME_REMOVED:
        return OUTCOME_REMOVED
    if new in _ATTEMPT_OUTCOMES:
        return new
    return existing if existing is not None else new


def merge_action(existing: str, new: str) -> str:
    """The higher of the two Tier 2 actions; any other action is kept."""
    old_rank = _ACTION_RANK.get(existing)
    new_rank = _ACTION_RANK.get(new)
    if old_rank is None or new_rank is None:
        return existing
    return new if new_rank > old_rank else existing


def _max_score(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def merge_incident(existing: Incident, new: Incident) -> Incident:
    """A repeat verdict on the same message: merge, never erase.

    The snapshot fields - subject, room, event, courses, text, the time it
    was first recorded - are the first write's and stay so. A text or a rule
    the first write did not have is filled in, never replaced.
    """
    categories = list(existing.categories)
    for category in new.categories:
        if category not in categories:
            categories.append(category)
    outcome = merge_outcome(existing.outcome, new.outcome)
    attempt = (existing.attempt_id, existing.prior_outcome, existing.prior_attempt_id)
    if new.outcome == OUTCOME_PENDING and outcome == OUTCOME_PENDING:
        # A new attempt begins and owns the outcome from here on; what the row
        # said before it is kept, to be put back if the attempt is not sent.
        attempt = (new.attempt_id, existing.outcome, existing.attempt_id)
    return attr.evolve(
        existing,
        action=merge_action(existing.action, new.action),
        outcome=outcome,
        attempt_id=attempt[0],
        prior_outcome=attempt[1],
        prior_attempt_id=attempt[2],
        categories=tuple(categories),
        self_harm=existing.self_harm or new.self_harm,
        top_score=_max_score(existing.top_score, new.top_score),
        text=existing.text if existing.text is not None else new.text,
        rule=existing.rule if existing.rule is not None else new.rule,
        room_name=(
            existing.room_name if existing.room_name is not None else new.room_name
        ),
    )


def _same_content(a: Incident, b: Incident) -> bool:
    return attr.evolve(a, updated_ms=0) == attr.evolve(b, updated_ms=0)


def next_updated_ms(previous: int, now_ms: int) -> int:
    """Strictly later than `previous`, so a mirror that keeps a row only when
    `updated_ms` grew never misses a change made in the same millisecond."""
    return max(now_ms, previous + 1)


# ---------------------------------------------------------------------------
# SQL. Literals, not formatted strings, for the reason in `disposition.py`.
# ---------------------------------------------------------------------------

# DOUBLE PRECISION rather than REAL for the score: on Postgres REAL is a
# four-byte float, which stores 0.9 as 0.899999976 - and the Safety page
# labels a score of 0.9 or more High.
_CREATE_INCIDENTS_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_safety_incidents (
        incident_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        action TEXT NOT NULL,
        outcome TEXT,
        subject_id TEXT,
        reporter_id TEXT,
        room_id TEXT NOT NULL,
        event_id TEXT,
        course_ids TEXT NOT NULL,
        categories TEXT NOT NULL,
        self_harm BOOLEAN NOT NULL,
        rule TEXT,
        top_score DOUBLE PRECISION,
        text TEXT,
        reason TEXT,
        created_ms BIGINT NOT NULL,
        updated_ms BIGINT NOT NULL,
        attempt_id TEXT,
        prior_outcome TEXT,
        prior_attempt_id TEXT,
        room_name TEXT
    )
"""

_CREATE_COURSES_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_safety_incident_courses (
        course_id TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        PRIMARY KEY (course_id, incident_id)
    )
"""

_CREATE_META_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_safety_incidents_meta (
        name TEXT PRIMARY KEY,
        done_ms BIGINT NOT NULL
    )
"""

_CREATE_OUTCOME_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS pangea_safety_incidents_outcome
    ON pangea_safety_incidents (outcome)
"""

_SELECT_ONE_SQL = """
    SELECT incident_id, source, action, outcome, subject_id, reporter_id,
        room_id, event_id, course_ids, categories, self_harm, rule, top_score,
        text, reason, created_ms, updated_ms, attempt_id, prior_outcome,
        prior_attempt_id, room_name
    FROM pangea_safety_incidents
    WHERE incident_id = ?
"""

_INSERT_SQL = """
    INSERT INTO pangea_safety_incidents
        (incident_id, source, action, outcome, subject_id, reporter_id,
        room_id, event_id, course_ids, categories, self_harm, rule, top_score,
        text, reason, created_ms, updated_ms, attempt_id, prior_outcome,
        prior_attempt_id, room_name)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (incident_id) DO NOTHING
"""

_INSERT_COURSE_SQL = """
    INSERT INTO pangea_safety_incident_courses (course_id, incident_id)
    VALUES (?, ?)
    ON CONFLICT (course_id, incident_id) DO NOTHING
"""

_UPDATE_SQL = """
    UPDATE pangea_safety_incidents
    SET action = ?, outcome = ?, categories = ?, self_harm = ?, rule = ?,
        top_score = ?, text = ?, updated_ms = ?, attempt_id = ?,
        prior_outcome = ?, prior_attempt_id = ?, room_name = ?
    WHERE incident_id = ?
"""

_SELECT_FOR_COURSE_SQL = """
    SELECT i.incident_id, i.source, i.action, i.outcome, i.subject_id,
        i.reporter_id, i.room_id, i.event_id, i.course_ids, i.categories,
        i.self_harm, i.rule, i.top_score, i.text, i.reason, i.created_ms,
        i.updated_ms, i.attempt_id, i.prior_outcome, i.prior_attempt_id,
        i.room_name
    FROM pangea_safety_incidents AS i
    INNER JOIN pangea_safety_incident_courses AS c
        ON c.incident_id = i.incident_id
    WHERE c.course_id = ?
    ORDER BY i.created_ms, i.incident_id
"""

_SELECT_PENDING_SQL = """
    SELECT incident_id, event_id, attempt_id FROM pangea_safety_incidents
    WHERE outcome = ?
"""

_SELECT_META_SQL = """
    SELECT done_ms FROM pangea_safety_incidents_meta WHERE name = ?
"""

_INSERT_META_SQL = """
    INSERT INTO pangea_safety_incidents_meta (name, done_ms) VALUES (?, ?)
    ON CONFLICT (name) DO NOTHING
"""

#: Every statement above, for the drift test.
STATEMENTS = (
    _CREATE_INCIDENTS_SQL,
    _CREATE_COURSES_SQL,
    _CREATE_META_SQL,
    _CREATE_OUTCOME_INDEX_SQL,
    _SELECT_ONE_SQL,
    _INSERT_SQL,
    _INSERT_COURSE_SQL,
    _UPDATE_SQL,
    _SELECT_FOR_COURSE_SQL,
    _SELECT_PENDING_SQL,
    _SELECT_META_SQL,
    _INSERT_META_SQL,
)


def _row_to_incident(row: Sequence[Any]) -> Incident:
    return Incident(
        incident_id=row[0],
        source=row[1],
        action=row[2],
        outcome=row[3],
        subject_id=row[4],
        reporter_id=row[5],
        room_id=row[6],
        event_id=row[7],
        course_ids=tuple(json.loads(row[8])),
        categories=tuple(json.loads(row[9])),
        self_harm=bool(row[10]),
        rule=row[11],
        top_score=None if row[12] is None else float(row[12]),
        text=row[13],
        reason=row[14],
        created_ms=int(row[15]),
        updated_ms=int(row[16]),
        attempt_id=row[17],
        prior_outcome=row[18],
        prior_attempt_id=row[19],
        room_name=row[20],
    )


def _insert_args(incident: Incident) -> Tuple[Any, ...]:
    return (
        incident.incident_id,
        incident.source,
        incident.action,
        incident.outcome,
        incident.subject_id,
        incident.reporter_id,
        incident.room_id,
        incident.event_id,
        json.dumps(list(incident.course_ids or ())),
        json.dumps(list(incident.categories)),
        bool(incident.self_harm),
        incident.rule,
        incident.top_score,
        nul_safe(incident.text),
        nul_safe(incident.reason),
        incident.created_ms,
        incident.updated_ms,
        incident.attempt_id,
        incident.prior_outcome,
        incident.prior_attempt_id,
        nul_safe(incident.room_name),
    )


class IncidentStore:
    """Reads and writes `pangea_safety_incidents`."""

    def __init__(self, homeserver: Any) -> None:
        self._hs = homeserver
        self._table_ready = False

    def _pool(self) -> Any:
        return self._hs.get_datastores().main.db_pool

    def now_ms(self) -> int:
        return int(self._hs.get_clock().time_msec())

    async def ensure_table(self) -> None:
        if self._table_ready:
            return

        def _create(txn: Any) -> None:
            txn.execute(_CREATE_INCIDENTS_SQL)
            txn.execute(_CREATE_COURSES_SQL)
            txn.execute(_CREATE_META_SQL)
            txn.execute(_CREATE_OUTCOME_INDEX_SQL)

        await self._pool().runInteraction(
            "pangea_safety_incidents_create_tables", _create
        )
        self._table_ready = True

    # ------------------------------------------------------------------
    # Writes. Each RAISES on failure: whether a failure is fatal to the
    # caller's next step - a redaction, a 200 - is the caller's decision.
    # ------------------------------------------------------------------

    async def insert(self, incident: Incident) -> Incident:
        """Write a new incident, or leave an existing one untouched.

        Returns the row as stored, which is the EXISTING row when there was
        one: a report retried with the same id is answered from the first
        write, and a backfill never overwrites a live verdict.
        """
        _require_courses(incident)
        await self.ensure_table()

        def _insert(txn: Any) -> Incident:
            txn.execute(_SELECT_ONE_SQL, (incident.incident_id,))
            row = txn.fetchone()
            if row is not None:
                return _row_to_incident(row)
            _write_new(txn, incident)
            return incident

        return await self._pool().runInteraction(
            "pangea_safety_incidents_insert", _insert
        )

    async def upsert_verdict(self, incident: Incident) -> Optional[str]:
        """Write a Tier 2 verdict, merging into any earlier one.

        Returns the outcome stored BEFORE this write, so a redaction that is
        then not sent can put back what an earlier attempt had established.
        """
        _require_courses(incident)
        await self.ensure_table()
        now_ms = self.now_ms()

        def _upsert(txn: Any) -> Optional[str]:
            txn.execute(_SELECT_ONE_SQL, (incident.incident_id,))
            row = txn.fetchone()
            if row is None:
                _write_new(txn, incident)
                return None
            existing = _row_to_incident(row)
            merged = merge_incident(existing, incident)
            if not _same_content(existing, merged):
                _write_update(txn, merged, next_updated_ms(existing.updated_ms, now_ms))
            return existing.outcome

        return await self._pool().runInteraction(
            "pangea_safety_incidents_upsert_verdict", _upsert
        )

    async def set_outcome(
        self,
        incident_id: str,
        outcome: str,
        *,
        attempt_id: Optional[str] = None,
        only_if: Optional[str] = None,
    ) -> bool:
        """Record how a redaction attempt ended.

        `removed` is final and is never replaced. `attempt_id` scopes the
        write to the attempt that still owns the row, and `only_if` to an
        outcome the row still has - which is what the startup sweep needs to
        settle a crashed attempt without touching a live one.

        Returns whether the row changed.
        """
        await self.ensure_table()
        now_ms = self.now_ms()

        def _set(txn: Any) -> bool:
            txn.execute(_SELECT_ONE_SQL, (incident_id,))
            row = txn.fetchone()
            if row is None:
                return False
            existing = _row_to_incident(row)
            if attempt_id is not None and existing.attempt_id != attempt_id:
                if (
                    existing.prior_attempt_id == attempt_id
                    and existing.prior_outcome != OUTCOME_REMOVED
                ):
                    # The owner a provisional attempt suspended: its result
                    # is what that attempt puts back if it is abandoned, so it
                    # is kept there rather than lost.
                    _write_update(
                        txn,
                        attr.evolve(existing, prior_outcome=outcome),
                        next_updated_ms(existing.updated_ms, now_ms),
                    )
                    return True
                # A later attempt owns the outcome now; this result is stale.
                return False
            if only_if is not None and existing.outcome != only_if:
                return False
            if existing.outcome == OUTCOME_REMOVED or existing.outcome == outcome:
                return False
            # The attempt has a result, so there is nothing left to put back:
            # the suspended owner's record goes, and its late result is stale.
            _write_update(
                txn,
                attr.evolve(
                    existing, outcome=outcome, prior_outcome=None, prior_attempt_id=None
                ),
                next_updated_ms(existing.updated_ms, now_ms),
            )
            return True

        return await self._pool().runInteraction(
            "pangea_safety_incidents_set_outcome", _set
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(self, incident_id: str) -> Optional[Incident]:
        await self.ensure_table()

        def _get(txn: Any) -> Optional[Incident]:
            txn.execute(_SELECT_ONE_SQL, (incident_id,))
            row = txn.fetchone()
            return None if row is None else _row_to_incident(row)

        return await self._pool().runInteraction("pangea_safety_incidents_get", _get)

    async def for_course(self, course_id: str) -> List[Incident]:
        """Every incident whose courses include `course_id`, complete and in
        a stable order: `created_ms`, then `incident_id`."""
        await self.ensure_table()

        def _select(txn: Any) -> List[Incident]:
            txn.execute(_SELECT_FOR_COURSE_SQL, (course_id,))
            return [_row_to_incident(row) for row in txn.fetchall()]

        return await self._pool().runInteraction(
            "pangea_safety_incidents_for_course", _select
        )

    async def abandon_attempt(self, incident_id: str, attempt_id: str) -> bool:
        """An attempt that will not send: put back what the row said before
        it began - the earlier attempt's outcome AND its ownership, so that
        attempt's own late result can still land - or `skipped` when there
        was nothing. A no-op once another attempt owns the row, or once it
        says `removed`. Returns whether the row changed."""
        await self.ensure_table()
        now_ms = self.now_ms()

        def _abandon(txn: Any) -> bool:
            txn.execute(_SELECT_ONE_SQL, (incident_id,))
            row = txn.fetchone()
            if row is None:
                return False
            existing = _row_to_incident(row)
            if existing.attempt_id != attempt_id:
                return False
            if existing.outcome == OUTCOME_REMOVED:
                return False
            restored = attr.evolve(
                existing,
                outcome=(
                    existing.prior_outcome
                    if existing.prior_outcome is not None
                    else OUTCOME_SKIPPED
                ),
                attempt_id=existing.prior_attempt_id,
                prior_outcome=None,
                prior_attempt_id=None,
            )
            _write_update(txn, restored, next_updated_ms(existing.updated_ms, now_ms))
            return True

        return await self._pool().runInteraction(
            "pangea_safety_incidents_abandon_attempt", _abandon
        )

    async def pending(self) -> List[Tuple[str, Optional[str], Optional[str]]]:
        """`(incident_id, event_id, attempt_id)` of every row left
        `pending`."""
        await self.ensure_table()

        def _select(txn: Any) -> List[Tuple[str, Optional[str], Optional[str]]]:
            txn.execute(_SELECT_PENDING_SQL, (OUTCOME_PENDING,))
            return [(row[0], row[1], row[2]) for row in txn.fetchall()]

        return await self._pool().runInteraction(
            "pangea_safety_incidents_pending", _select
        )

    async def is_done(self, name: str) -> bool:
        await self.ensure_table()

        def _select(txn: Any) -> bool:
            txn.execute(_SELECT_META_SQL, (name,))
            return txn.fetchone() is not None

        return await self._pool().runInteraction(
            "pangea_safety_incidents_meta_read", _select
        )

    async def mark_done(self, name: str) -> None:
        await self.ensure_table()
        now_ms = self.now_ms()

        def _insert(txn: Any) -> None:
            txn.execute(_INSERT_META_SQL, (name, now_ms))

        await self._pool().runInteraction("pangea_safety_incidents_meta_write", _insert)


def _require_courses(incident: Incident) -> None:
    if incident.course_ids is None:
        # A programming error rather than a data one: the caller resolves the
        # courses first, and a row written with none would reach no Safety
        # page while looking recorded.
        raise ValueError("an incident is never written before its courses")


def _execute_private(txn: Any, sql: str, args: Tuple[Any, ...]) -> None:
    """Run a statement whose arguments carry message text or a reason.

    Below Synapse's `LoggingTransaction`, on the cursor it wraps: that
    wrapper logs every statement's arguments to `synapse.storage.SQL` at
    DEBUG, which would put a learner's message and a reporter's reason into
    the homeserver log of any deployment that turns SQL logging up. The
    parameter style is converted exactly as the wrapper converts it.
    """
    engine = txn.database_engine
    failure: Optional[BaseException] = None
    try:
        txn.txn.execute(engine.convert_param_style(sql), args)
        return
    except Exception as exc:
        if engine.is_deadlock(exc) or isinstance(exc, engine.module.OperationalError):
            # Synapse retries these, and needs the driver's own exception to
            # do it. Neither carries a row: a serialisation failure names no
            # values, and an operational error is about the connection.
            raise
        failure = exc
    # Anything else is replaced by its type name, raised OUTSIDE the handler
    # so the driver's exception is not attached as context. A constraint
    # failure's message can quote the whole failing row ("Failing row
    # contains ..."), and Synapse logs a failed transaction's exception
    # message to `synapse.storage.txn`.
    raise IncidentWriteError(type(failure).__name__)


class IncidentWriteError(Exception):
    """A write of an incident failed. Carries the driver's exception TYPE and
    nothing else, because the driver's message can quote the row."""


def _write_new(txn: Any, incident: Incident) -> None:
    _execute_private(txn, _INSERT_SQL, _insert_args(incident))
    for course_id in incident.course_ids or ():
        txn.execute(_INSERT_COURSE_SQL, (course_id, incident.incident_id))


def _write_update(txn: Any, incident: Incident, updated_ms: int) -> None:
    _execute_private(
        txn,
        _UPDATE_SQL,
        (
            incident.action,
            incident.outcome,
            json.dumps(list(incident.categories)),
            bool(incident.self_harm),
            incident.rule,
            incident.top_score,
            nul_safe(incident.text),
            updated_ms,
            incident.attempt_id,
            incident.prior_outcome,
            incident.prior_attempt_id,
            nul_safe(incident.room_name),
            incident.incident_id,
        ),
    )


def unique(values: Iterable[str]) -> Tuple[str, ...]:
    """In first-seen order, each once."""
    seen: List[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return tuple(seen)
