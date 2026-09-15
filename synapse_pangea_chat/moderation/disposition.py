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
table created on first use through `db_pool.runInteraction`, with `%s`
placeholders, which Synapse's transaction rewrites for SQLite and passes
through for Postgres.
"""

import uuid
from collections import OrderedDict
from typing import Any, Optional

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

_INSERT_SQL = """
    INSERT INTO pangea_moderation_disposition
        (event_id, room_id, disposition, category, decided_at_ms, claim_id)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (event_id) DO NOTHING
"""

_SELECT_SQL = """
    SELECT disposition, claim_id FROM pangea_moderation_disposition
    WHERE event_id = %s
"""

_DELETE_CLAIM_SQL = """
    DELETE FROM pangea_moderation_disposition
    WHERE event_id = %s AND disposition = %s
"""

#: Every statement above, for the drift test.
STATEMENTS = (_CREATE_TABLE_SQL, _INSERT_SQL, _SELECT_SQL, _DELETE_CLAIM_SQL)


class DispositionStore:
    """Reads and writes `pangea_moderation_disposition`."""

    def __init__(self, homeserver: Any) -> None:
        self._hs = homeserver
        self._table_ready = False
        self._remembered: "OrderedDict[str, None]" = OrderedDict()

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

        `ON CONFLICT DO NOTHING`, so the FIRST decision stands: a preserve
        that arrives twice is one row, and nothing can overwrite a row that
        already says preserved. Returns whether the durable write landed - the
        caller counts a failure, because a guarantee that is not on disk is
        not durable and an operator has to be able to see that.
        """
        # Remembered first and unconditionally. If the write below fails this
        # is all that stands between the disclosure and the next verdict in
        # this process, and it must not depend on the write succeeding.
        self._remember(event_id)
        try:
            await self._ensure_table()

            def _insert(txn: Any) -> None:
                txn.execute(
                    _INSERT_SQL,
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
                "pangea_moderation_record_disposition", _insert
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
                "at %s (%s); the message stays up but a restart will not know "
                "why",
                event_id,
                room_id,
                error_site(exc),
                type(exc).__name__,
            )
            return False

    async def claim_redaction(
        self, *, event_id: str, room_id: str, category: str
    ) -> Optional[str]:
        """Take the right to redact this event, or find out who has it.

        Returns `GRANTED`, `PRESERVED`, `REDACTED`, or **None for "we could
        not find out"**. Three outcomes plus an unknown, because two would
        force the caller to guess and the wrong guess deletes a disclosure.

        **One atomic write, not a read and then a write.** A read-then-redact
        has a window: two verdicts on the same event, in two instances, both
        read a table that says nothing, and then one writes `preserved` while
        the other sends the redaction. `INSERT ... ON CONFLICT DO NOTHING`
        followed by a read of the row IN THE SAME TRANSACTION closes it -
        exactly one of the two decisions lands, and the loser is told which
        one won. Preserving wins whenever it got there first, which is the
        guarantee; the reverse order is the limit ADR-7/OD-14 already records,
        that a first evaluation can simply miss a disclosure.

        It also makes a redaction idempotent ACROSS processes, which nothing
        in memory could: the in-flight set the dispatcher keeps is per-process,
        so two instances both configured to run background tasks used to send
        two redactions for one message.
        """
        if event_id in self._remembered:
            return PRESERVED
        try:
            await self._ensure_table()

            claim_id = uuid.uuid4().hex

            def _claim(txn: Any) -> Any:
                txn.execute(
                    _INSERT_SQL,
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
            return PRESERVED
        # Did THIS call insert the row, or was it already there? `rowcount`
        # after an `ON CONFLICT DO NOTHING` would answer it directly and is
        # not portable enough to rely on across the two engines and the two
        # Synapse pins this module supports, so the row carries the id of the
        # claim that wrote it. A random id and nothing derived from the room,
        # the sender or the text.
        return GRANTED if row[1] == claim_id else REDACTED

    async def release_redaction_claim(self, event_id: str) -> None:
        """Give the claim back when the redaction did not happen.

        A claim that outlives a failed send would block every later attempt on
        an event that is still standing - turning a transient send failure
        into a permanent one. Only a `redacted` claim is released; a
        `preserved` row is never removed by anything.
        """
        try:
            await self._ensure_table()

            def _release(txn: Any) -> None:
                txn.execute(_DELETE_CLAIM_SQL, (event_id, REDACTED))

            await self._pool().runInteraction(
                "pangea_moderation_release_redaction_claim", _release
            )
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: the cost is one message that will not be re-tried,
            # which is the behaviour before this table existed.
            logger.warning(
                "tier2 could not release the redaction claim on %s at %s (%s)",
                event_id,
                error_site(exc),
                type(exc).__name__,
            )

    def _remember(self, event_id: str) -> None:
        self._remembered[event_id] = None
        self._remembered.move_to_end(event_id)
        while len(self._remembered) > _MEMORY_SIZE:
            self._remembered.popitem(last=False)
