"""The durable record of what Tier 2 decided about an event.

One table, one decision, and one guarantee it exists to make:

> Once an event has been recorded as PRESERVED, nothing may ever redact it.
> Not a later verdict, not a retry, not a restart, not a second worker.

`self_harm` is the category that needs it. Deleting a disclosure of self-harm
is itself a harm - the learner is asking for help, the message is the only
record that they did, and a redaction removes it from the room while telling
nobody - so ADR-7 leaves the message standing. "Never" is the word that makes
that a guarantee rather than a default, and "never" cannot rest on anything a
process can lose.

**What it replaced, and why memory was not enough.** Preserving recorded no
decision at all. The only thing between a protected disclosure and a redaction
was the dispatcher's in-flight claim, which is released the moment the job
finishes - so delivering the same event twice, with `self-harm/intent` first
and `harassment` second, redacted it. A restart lost the protection for the
same reason, and two instances never had it: two processes, two memories, no
shared claim. An earlier revision proposed an LRU of remembered verdicts,
which fails on eviction as well.

**A claim that cannot be given back is a message that can never be taken
down.** The release is scoped and retried by nothing, so a release that fails
- or a process killed between the claim committing and the send - leaves a
durable `redacted` row on a message nobody redacted. There is no expiry and
no reconciliation here: a claim with a time limit is a second way to
double-redact, and choosing between those needs evidence this change does not
have. It is counted (`pangea_moderation_tier2_claim_stranded_total`) and
logged at ERROR so the row can be cleared by hand.

**The limit, stated exactly, because it is the one the table cannot close.**
A redaction is an irreversible action taken in another system, and no table
can recall one that is already in flight. So the guarantee is about the
ORDER OF DECISIONS: once an event is recorded preserved, no later decision
may redact it. A preserve that arrives AFTER the redaction decision was
taken - in the window between the claim committing and the send landing -
cannot undo it.

Within one instance that window is unreachable: the dispatcher holds an
in-flight claim per event id, so there is never a second verdict for the same
event at the same time. It is reachable only when two instances both run
background tasks, which is the misconfiguration `should_run_background_tasks`
exists to prevent and which `pangea_moderation_tier2_active` makes visible -
`sum(...) > 1` is the alert. When it does happen the redaction is counted
under `pangea_moderation_tier2_redacted_after_preserve_total` and logged at
ERROR with the room and event, because a disclosure was removed and the only
remaining remedy is a human who knows.

**It does not fail open, and that is deliberate.** Everywhere else in this
module an unknown means "leave the message alone and carry on"; here, an
unknown means "do not redact". A message left standing can still be redacted
by a human; a deleted disclosure cannot be brought back, and the person it
belonged to is the person least able to absorb the mistake. So a disposition
we cannot read is a redaction we do not send, and it is counted.

**What it stores.** `room_id`, `event_id`, the normalised category and a
timestamp. Never the sender's Matrix ID and never a word of the message: this
is a safeguarding record, read by people, and ADR-7c says what belongs in one.

The shape follows `delete_user.pangea_delete_user_schedule` - a module-owned
table created on first use through `db_pool.runInteraction`.

**Placeholders are `?`, which is Synapse's canonical style and not Postgres's.**
`Sqlite3Engine.convert_param_style` returns the SQL UNCHANGED and
`PostgresEngine.convert_param_style` rewrites `?` into `%s`, so a module that
writes `%s` runs on Postgres and raises `OperationalError: near "%"` on
SQLite - which an end-to-end test that only ever boots Postgres cannot see.
"""

import uuid
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from synapse_pangea_chat.moderation import metrics
from synapse_pangea_chat.moderation.compat import reraise_if_cancelled
from synapse_pangea_chat.moderation.log_safety import error_site, scrubbing_logger

logger = scrubbing_logger("synapse.modules.synapse_pangea_chat.moderation.disposition")

DISPOSITION_TABLE = "pangea_moderation_disposition"

#: The two decisions, and the row holds exactly one of them for the life of
#: the event. A column rather than a boolean so a later decision - escalated,
#: reviewed, actioned - is a new value rather than a new table.
PRESERVED = "preserved"
REDACTED = "redacted"

#: `claim_redaction` returning this means the caller owns the redaction.
GRANTED = "granted"

#: How many recently preserved event ids this process keeps in memory.
#:
#: A CACHE in front of the table, never the guarantee. It exists for one case:
#: the durable write failed, so the table cannot answer for this event, and
#: without it a later verdict in this same process would redact a disclosure
#: we had just decided to protect. It can only ever ADD protection - a miss
#: falls through to the table, and a table read that fails refuses to redact.
#: Eviction therefore costs nothing the table already holds, and the write
#: failure that would make it matter is counted when it happens.
_MEMORY_SIZE = 8192

# The statements, as literals rather than as f-strings over `DISPOSITION_TABLE`.
# A table name cannot be a bound parameter, so interpolating it is the one
# thing SQL forces a caller to do by hand - and a query built by formatting is
# indistinguishable, to a reader or to bandit, from one built out of user
# input. Writing them out means there is no formatting here at all, and
# `test_every_statement_names_the_table` is what keeps the constant and the
# statements from drifting apart.
_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_moderation_disposition (
        event_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL,
        disposition TEXT NOT NULL,
        category TEXT NOT NULL,
        decided_at_ms BIGINT NOT NULL,
        claim_id TEXT NOT NULL
    )
"""

# The redaction claim. `DO NOTHING`, so an event already decided keeps its
# decision and the caller is told whose it is.
_CLAIM_SQL = """
    INSERT INTO pangea_moderation_disposition
        (event_id, room_id, disposition, category, decided_at_ms, claim_id)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (event_id) DO NOTHING
"""

# The preserve. `DO UPDATE`, and the asymmetry with the claim above is the
# whole point: **preserving always wins the row.** A redaction claim is
# PROVISIONAL - it is released when the send does not happen - so a preserve
# that arrived while one was outstanding and did nothing would be lost the
# moment the claim was released, and the next verdict would redact the
# disclosure. Preserving is final in the other direction: nothing overwrites
# a `preserved` row, which is what the `WHERE` clause says.
_PRESERVE_SQL = """
    INSERT INTO pangea_moderation_disposition
        (event_id, room_id, disposition, category, decided_at_ms, claim_id)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (event_id) DO UPDATE SET
        disposition = excluded.disposition,
        category = excluded.category,
        decided_at_ms = excluded.decided_at_ms,
        claim_id = excluded.claim_id
    WHERE pangea_moderation_disposition.disposition <> 'preserved'
"""

_SELECT_SQL = """
    SELECT disposition, claim_id FROM pangea_moderation_disposition
    WHERE event_id = ?
"""

# Scoped to OUR claim by id as well as by disposition. A release must never
# remove a `preserved` row, and it must never remove another instance's claim:
# a delete on the event id alone would do both.
_DELETE_CLAIM_SQL = """
    DELETE FROM pangea_moderation_disposition
    WHERE event_id = ? AND disposition = ? AND claim_id = ?
"""

#: Every statement above, for the drift test.
STATEMENTS = (
    _CREATE_TABLE_SQL,
    _CLAIM_SQL,
    _PRESERVE_SQL,
    _SELECT_SQL,
    _DELETE_CLAIM_SQL,
)


class DispositionStore:
    """Reads and writes `pangea_moderation_disposition`."""

    def __init__(self, homeserver: Any) -> None:
        self._hs = homeserver
        self._table_ready = False
        self._remembered: "OrderedDict[str, None]" = OrderedDict()
        # Preserves whose durable write has not landed. NOT bounded, and
        # deliberately: this holds only the decisions the database refused,
        # every one of them is retried on the next operation, and dropping one
        # to save memory would drop the guarantee it carries. While the
        # database is refusing writes it is also refusing the claim reads, so
        # nothing is being redacted anyway.
        self._pending: Dict[str, Tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _ensure_table(self) -> None:
        if self._table_ready:
            return

        def _create(txn: Any) -> None:
            txn.execute(_CREATE_TABLE_SQL)

        await self._pool().runInteraction(
            "pangea_moderation_create_disposition_table", _create
        )
        self._table_ready = True

    def _pool(self) -> Any:
        return self._hs.get_datastores().main.db_pool

    def _now_ms(self) -> int:
        return int(self._hs.get_clock().time_msec())

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    async def record_preserved(
        self, *, event_id: str, room_id: str, category: str
    ) -> bool:
        """Record that this event must never be redacted. Idempotent.

        **It takes the row from an outstanding redaction claim**, which a
        plain `DO NOTHING` did not: a claim is provisional and is released
        when the send does not happen, so a preserve that arrived while one
        was outstanding wrote nothing, reported success, and was gone the
        moment the claim was released - and the next verdict redacted the
        disclosure. Nothing overwrites a `preserved` row in the other
        direction.

        Returns whether the durable write landed. A write that failed is
        remembered and RETRIED on the next operation, because the alternative
        is a guarantee that lives only in this process's memory until the row
        happens to be written by somebody.

        **The retry is in memory, so a restart before it lands loses it**, and
        a later verdict on that event will then redact the disclosure. There
        is no closing that from here: the only durable place to record "we
        could not record this" is the store that just refused. It is counted
        (`pangea_moderation_tier2_disposition_write_failed_total`) and logged
        at ERROR, and while the store is refusing it is also refusing every
        claim, so nothing is being redacted in the meantime.
        """
        # Remembered first and unconditionally. If the write below fails this
        # is all that stands between the disclosure and the next verdict in
        # this process, and it must not depend on the write succeeding.
        self._remember(event_id)
        self._pending[event_id] = (room_id, category)
        return await self._flush_pending(event_id)

    async def _flush_pending(self, event_id: Optional[str] = None) -> bool:
        """Write the preserves that have not landed yet.

        Called before every claim as well as on the preserve itself, so a
        database that was briefly unavailable cannot leave a disclosure
        unprotected once it comes back - in THIS process: the claim that would
        redact it writes the preserve first and then loses the row to it. A
        restart before the retry lands loses the decision; see
        `record_preserved`.

        **A preserve that can never be written wedges every redaction in this
        process**, because a claim is refused while anything is pending. That
        is the safe direction and it is a whole-feature outage from one row,
        so it is said here rather than discovered: a store that refuses a
        write but serves reads is exotic, and a store that refuses both
        already stops every claim at `_ensure_table`.
        """
        if not self._pending:
            return True
        for pending_id, (room_id, category) in list(self._pending.items()):
            if not await self._write_preserve(pending_id, room_id, category):
                # Stop on the first failure rather than retrying the whole
                # backlog against a database that has just refused one: the
                # rest are retried on the next operation, and hammering a
                # database that is down is how a moderation problem becomes a
                # homeserver problem.
                break
            self._pending.pop(pending_id, None)
        if event_id is not None:
            # The caller asked about one decision: it landed if it is no
            # longer waiting, whatever happened to the rest of the backlog.
            return event_id not in self._pending
        return not self._pending

    async def _write_preserve(self, event_id: str, room_id: str, category: str) -> bool:
        try:
            await self._ensure_table()

            def _preserve(txn: Any) -> None:
                txn.execute(
                    _PRESERVE_SQL,
                    (
                        event_id,
                        room_id,
                        PRESERVED,
                        category,
                        self._now_ms(),
                        uuid.uuid4().hex,
                    ),
                )

            await self._pool().runInteraction(
                "pangea_moderation_record_disposition", _preserve
            )
            return True
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: the message is preserved either way - this is the
            # record of the decision, not the decision. Type and site rather
            # than a traceback, for the reason given throughout this module.
            metrics.TIER2_DISPOSITION_WRITE_FAILED.inc()
            logger.error(
                "tier2 could not record the preserved disposition for %s in %s "
                "at %s (%s); the message stays up and the write will be "
                "retried, but a restart before it lands will not know why",
                event_id,
                room_id,
                error_site(exc),
                type(exc).__name__,
            )
            return False

    async def claim_redaction(
        self, *, event_id: str, room_id: str, category: str
    ) -> Optional[Tuple[str, str]]:
        """Take the right to redact this event, or find out who has it.

        Returns `(outcome, claim_id)` where outcome is `GRANTED`, `PRESERVED`
        or `REDACTED`, or **None for "we could not find out"**. Three outcomes
        plus an unknown, because two would force the caller to guess and the
        wrong guess deletes a disclosure. The claim id goes back to the caller
        so a release can be scoped to the claim it is giving back.

        **One atomic write, not a read and then a write.** A read-then-redact
        has a window: two verdicts on the same event, in two instances, both
        read a table that says nothing, and then one writes `preserved` while
        the other sends the redaction. `INSERT ... ON CONFLICT DO NOTHING`
        followed by a read of the row IN THE SAME TRANSACTION closes it -
        exactly one of the two decisions lands, and the loser learns that it
        lost. Preserving wins whenever it got there first, which is the
        guarantee; the reverse order is the limit ADR-7/OD-14 already records,
        that a first evaluation can simply miss a disclosure.

        **What the loser is told depends on the isolation level, and the safe
        answer does not.** Synapse sets REPEATABLE READ on its Postgres
        connections, so the loser's `SELECT` reads the snapshot its
        transaction opened with and may not see the row the winner has just
        committed. It then reads `row is None` and reports an unknown
        disposition rather than naming the winner. Both outcomes refuse to
        redact - which is the guarantee - and the difference is which metric
        an operator sees, so it is written down here rather than left to be
        inferred from a docstring that assumed READ COMMITTED.

        It also makes a redaction idempotent ACROSS processes, which nothing
        in memory could: the in-flight set the dispatcher keeps is per-process,
        so two instances both configured to run background tasks used to send
        two redactions for one message.
        """
        if event_id in self._remembered:
            return (PRESERVED, "")
        # Preserves the database refused earlier are written FIRST, so a
        # database that was briefly unavailable cannot leave a disclosure
        # unprotected once it comes back: the row exists before this claim
        # asks for it, and the claim then loses to it. A flush that fails
        # means we still cannot establish the dispositions we hold, which is
        # an unknown, which is never a redaction.
        if not await self._flush_pending():
            return None
        try:
            await self._ensure_table()

            claim_id = uuid.uuid4().hex

            def _claim(txn: Any) -> Any:
                txn.execute(
                    _CLAIM_SQL,
                    (
                        event_id,
                        room_id,
                        REDACTED,
                        category,
                        self._now_ms(),
                        claim_id,
                    ),
                )
                txn.execute(_SELECT_SQL, (event_id,))
                return txn.fetchone()

            row = await self._pool().runInteraction(
                "pangea_moderation_claim_redaction", _claim
            )
        except Exception as exc:
            reraise_if_cancelled(exc)
            # NOT fail-open in the sense of carrying on to the redaction: an
            # unknown disposition is never a redaction. See the module
            # docstring for why this one decision goes the other way.
            logger.warning(
                "tier2 could not claim the disposition of %s at %s (%s); "
                "not redacting",
                event_id,
                error_site(exc),
                type(exc).__name__,
            )
            return None
        if row is None:
            # The row we just inserted is not there, which means the write did
            # not land. Treated as an unknown rather than as permission.
            return None
        if row[0] == PRESERVED:
            # Remembered on the way back so a repeat verdict on a hot event
            # does not pay for a second read.
            self._remember(event_id)
            return (PRESERVED, "")
        # Did THIS call insert the row, or was it already there? `rowcount`
        # after an `ON CONFLICT DO NOTHING` would answer it directly and is
        # not portable enough to rely on across the two engines and the two
        # Synapse pins this module supports, so the row carries the id of the
        # claim that wrote it. A random id and nothing derived from the room,
        # the sender or the text.
        return (GRANTED, claim_id) if row[1] == claim_id else (REDACTED, "")

    async def is_preserved(self, event_id: str) -> Optional[bool]:
        """Read the row and say nothing else. True, False, or None for "we
        could not find out".

        A plain read, with no claim: this is what a caller asks AFTER an
        action, to find out whether a decision landed while it was busy. It
        must not take a row of its own, or asking the question would change
        the answer.
        """
        if event_id in self._remembered:
            return True
        try:
            await self._ensure_table()

            def _select(txn: Any) -> Any:
                txn.execute(_SELECT_SQL, (event_id,))
                return txn.fetchone()

            row = await self._pool().runInteraction(
                "pangea_moderation_read_disposition", _select
            )
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: the caller uses this to REPORT, not to decide.
            logger.warning(
                "tier2 could not read the disposition of %s at %s (%s)",
                event_id,
                error_site(exc),
                type(exc).__name__,
            )
            return None
        if row is None:
            return False
        if row[0] == PRESERVED:
            self._remember(event_id)
            return True
        return False

    async def release_redaction_claim(self, event_id: str, claim_id: str) -> None:
        """Give OUR claim back when the redaction did not happen.

        A claim that outlives a failed send would block every later attempt on
        an event that is still standing - turning a transient send failure
        into a permanent one.

        Scoped by claim id as well as by disposition, so a release cannot
        remove a `preserved` row that took the claim's place in the meantime,
        and cannot remove a claim another instance is holding.
        """
        if not claim_id:
            return
        try:
            await self._ensure_table()

            def _release(txn: Any) -> None:
                txn.execute(_DELETE_CLAIM_SQL, (event_id, REDACTED, claim_id))

            await self._pool().runInteraction(
                "pangea_moderation_release_redaction_claim", _release
            )
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok in the sense that nothing is blocked, and NOT in the
            # sense that nothing is lost: the row still says this event was
            # redacted and it was not, so no verdict on any instance will
            # ever take that message down again. That is worse than the
            # behaviour before this table existed, where a later verdict
            # could still act, so it is counted rather than logged alone -
            # clearing the row is a human's job and they have to know.
            metrics.TIER2_CLAIM_STRANDED.inc()
            logger.error(
                "tier2 could not release the redaction claim on %s at %s "
                "(%s); the row says it was redacted and it was not, so "
                "nothing will take that message down until the row is cleared",
                event_id,
                error_site(exc),
                type(exc).__name__,
            )

    def _remember(self, event_id: str) -> None:
        self._remembered[event_id] = None
        self._remembered.move_to_end(event_id)
        while len(self._remembered) > _MEMORY_SIZE:
            self._remembered.popitem(last=False)
