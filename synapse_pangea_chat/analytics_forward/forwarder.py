"""The forwarder loop: tail the event stream from a durable cursor, deliver.

One code path for live traffic and catch-up. ``on_new_event`` only wakes the
loop; the loop reads the next forwarded events above the cursor from
Synapse's own tables, sends them with any changed profiles, and stores the
new cursor only once the ingest has acknowledged the batch, which it does only
after committing it. Nothing is held in memory: during an outage the backlog
stays in Synapse's tables and the loop resumes from the cursor, retrying
forever with a capped backoff. A batch re-sent after a crash is a no-op at the
ingest, whose every write is idempotent.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, List, Optional, Set, Tuple

from synapse_pangea_chat.analytics_forward import metrics
from synapse_pangea_chat.analytics_forward.config import START_AT_CURRENT
from synapse_pangea_chat.analytics_forward.wire import (
    BadAck,
    check_ack,
    event_record,
    ingest_body,
    is_forwarded,
    profile_record,
)

logger = logging.getLogger("synapse.modules.synapse_pangea_chat.analytics_forward")

#: How far ``profiles_clean_before`` trails the read. A profile change is
#: written, then the callback marks the outbox, then that commits; a read in
#: between must not report the change as delivered. Milliseconds cover the
#: real gap; two seconds is the margin.
PROFILE_CLOCK_GRACE_MS = 2000
MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
#: The pause between reads while events keep arriving, so a busy homeserver
#: costs a few indexed reads a second rather than one per event.
BUSY_PAUSE_SECONDS = 0.25
#: The whole exchange, body included. Synapse's client bounds only the wait
#: for the response headers, and a body that never ends would stall the loop.
REQUEST_TIMEOUT_SECONDS = 60.0


class DeliveryError(Exception):
    """The ingest did not acknowledge the batch. ``reason`` is a metric label."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class Forwarder:
    def __init__(
        self,
        *,
        store: Any,
        post: Callable[[dict], Awaitable[Any]],
        consent: Any,
        clock_ms: Callable[[], int],
        sleep: Callable[[float], Awaitable[None]],
        run_in_background: Callable[[Callable[[], Awaitable[None]]], Any],
        batch_max: int,
        poll_seconds: int,
        start_position: Any = START_AT_CURRENT,
    ) -> None:
        self._store = store
        self._post = post
        self._consent = consent
        self._clock_ms = clock_ms
        self._sleep = sleep
        self._run_in_background = run_in_background
        self._batch_max = batch_max
        self._poll_ms = poll_seconds * 1000
        self._start_position = start_position
        self._cursor: Optional[int] = None
        self._last_post_ms: Optional[int] = None
        #: The highest stream position of a join made for a consented invite
        #: that the ingest has not yet been told about.
        self._fence = 0
        #: Consented invites whose join is owed: every invite into an analytics
        #: room seen in the stream or found pending. A separate worker task
        #: runs the proof and joins; until it has an answer for an invite, the
        #: invite is named in every batch (``pending_accepts``) so the ingest
        #: holds that teacher's Refresh. Delivery never waits on a join.
        self._owed: Set[Tuple[str, str]] = set()
        #: Grows whenever the owed set changes, so the ingest keeps only the
        #: newest set even when an older request lands late. Stored before any
        #: batch carrying it is sent, and bumped past the stored value on
        #: start, so a rev after a restart is newer than any sent before it.
        self._rev = 0
        self._saved_rev = 0
        self._failures = 0
        self._running = False
        self._woken = False

    @property
    def cursor(self) -> Optional[int]:
        return self._cursor

    @property
    def poll_ms(self) -> int:
        return self._poll_ms

    async def start(self) -> None:
        """Before anything is sent: re-send every profile, load the cursor,
        accept invites that were pending before the stream reaches them."""
        await self._store.mark_all_for_resend()
        if self._start_position == START_AT_CURRENT:
            initial = self._store.head()
        else:
            initial = int(self._start_position)
        cursor = await self._store.load_cursor(initial)
        # A consented join made before a restart may never have had its fence
        # delivered; any self-join into an analytics room still above the
        # cursor is fenced again (over-fencing only delays a Refresh).
        self._fence = max(
            self._fence, await self._store.undelivered_analytics_join(cursor)
        )
        # Every pending invite owed first, and a failed read fails the start
        # (which is retried): until the cursor is set, wake() sends nothing,
        # so no batch goes out without every owed join named in it.
        self._saved_rev = await self._store.load_pending_rev()
        self._rev = self._saved_rev + 1
        await self.scan_pending_invites()
        self._cursor = cursor
        logger.info("analytics forwarder starting at stream position %d", cursor)
        metrics.ACTIVE.set(1)
        self.wake()

    async def scan_pending_invites(self) -> None:
        """Owe the proof for every current pending invite into an analytics
        room. Raises if the read fails."""
        for room_id, user_id in await self._store.pending_analytics_invites():
            self._owe(room_id, user_id)

    async def daily_scan(self) -> None:
        try:
            await self.scan_pending_invites()
        except Exception:  # silent-ok: logged; what is already owed stays owed
            logger.exception("analytics forwarder: pending invite scan failed")

    def _owe(self, room_id: str, user_id: str) -> None:
        if (room_id, user_id) in self._owed:
            return
        self._owed.add((room_id, user_id))
        self._rev += 1
        # One task per invite: a join that hangs holds only its own invite.
        self._run_in_background(lambda: self.answer(room_id, user_id))

    async def answer(self, room_id: str, user_id: str) -> None:
        """Run the proof and join for one owed invite, retrying failures once
        per poll until there is an answer. The stream never waits on this."""
        while True:
            try:
                verdict, position = await self._consent.accept_if_consented(
                    room_id, user_id
                )
                break
            except Exception:  # silent-ok: logged, counted, still owed and named
                logger.exception("analytics forwarder: consented-invite join failed")
                metrics.CONSENT.labels(verdict="error").inc()
                await self._sleep(self._poll_ms / 1000)
        metrics.CONSENT.labels(verdict=verdict).inc()
        # Fence first, then release: a batch reads both at once.
        if position > self._fence:
            self._fence = position
        self._owed.discard((room_id, user_id))
        self._rev += 1
        self.wake()

    # -- the loop -----------------------------------------------------------

    def wake(self) -> None:
        """Called on every forwarded event and on the poll timer. No I/O."""
        if self._cursor is None:
            return
        self._woken = True
        if not self._running:
            self._running = True
            self._run_in_background(self.drain)

    async def drain(self) -> None:
        try:
            while True:
                self._woken = False
                try:
                    more = await self.step()
                except Exception as exc:
                    await self._sleep(self._backoff(exc))
                    continue
                self._failures = 0
                # Every read is followed by the pause, and a wake-up during it
                # joins this run rather than starting another: at most a few
                # reads a second, catch-up and live traffic alike.
                await self._sleep(BUSY_PAUSE_SECONDS)
                if not (more or self._woken):
                    return
        finally:
            self._running = False

    def _backoff(self, exc: Exception) -> float:
        reason = exc.reason if isinstance(exc, DeliveryError) else "error"
        metrics.FAILURES.labels(reason=reason).inc()
        self._failures += 1
        if reason == "auth":
            # A wrong secret is a config error: nothing gets through until an
            # operator fixes it, so it is logged loudly and retried slowly.
            logger.error(
                "analytics forwarder: the ingest refused the shared secret; "
                "check analytics_forward.secret on both sides"
            )
            return MAX_BACKOFF_SECONDS
        if isinstance(exc, DeliveryError):
            logger.warning("analytics forwarder: delivery failed: %s", exc)
        else:
            logger.exception("analytics forwarder: step failed")
        # The exponent is capped before it is used: a long outage counts
        # thousands of failures, and 2.0 ** 1024 overflows.
        doublings = min(self._failures - 1, 6)
        return min(MAX_BACKOFF_SECONDS, MIN_BACKOFF_SECONDS * 2**doublings)

    async def step(self) -> bool:
        """Read, send, and on acknowledgement advance. True when more is waiting."""
        assert self._cursor is not None
        head = self._store.head()
        events, reached = await self._store.read_events(
            self._cursor, head, self._batch_max
        )
        records: List[dict] = []
        for event in events:
            state_key = event.get_state_key() if event.is_state() else None
            if not is_forwarded(event.type, state_key):
                continue
            records.append(event_record(event))
            if (
                event.type == "m.room.member"
                and event.content.get("membership") == "invite"
                and state_key is not None
            ):
                # Owed, not joined here: the worker answers it, and this batch
                # already names it, so the ingest waits for the answer.
                self._owe(event.room_id, state_key)

        read_ms = self._clock_ms()
        dirty, oldest = await self._store.dirty_profiles(self._batch_max)
        heartbeat_due = (
            self._last_post_ms is None or read_ms - self._last_post_ms >= self._poll_ms
        )
        fence, owed, rev = self._fence, sorted(self._owed), self._rev
        full = len(events) >= self._batch_max
        if not (records or dirty or fence or full or heartbeat_due):
            return False

        profiles = await self._store.profiles([user_id for user_id, _ in dirty])
        oldest_change = read_ms if oldest is None else min(oldest, read_ms)
        clean_before = oldest_change - PROFILE_CLOCK_GRACE_MS
        body = ingest_body(
            events=records,
            profiles=[
                profile_record(user_id, revision, profiles.get(user_id))
                for user_id, revision in dirty
            ],
            delivered_through=reached,
            profiles_clean_before=clean_before,
            accept_join_pos=fence or None,
            pending_accepts=owed,
            pending_accepts_rev=rev,
        )
        if rev > self._saved_rev:
            await self._store.save_pending_rev(rev)
            self._saved_rev = rev
        response = await self._post(body)
        try:
            rejected = check_ack(response, reached)
        except BadAck as exc:
            raise DeliveryError("bad_ack", str(exc)) from exc

        await self._store.advance(reached, dirty)
        self._cursor = max(self._cursor, reached)
        self._last_post_ms = read_ms
        if self._fence == fence:
            self._fence = 0
        if rejected:
            metrics.REJECTED.inc(rejected)
        metrics.LAG.set(max(0, head - self._cursor))
        return full or len(dirty) >= self._batch_max


def make_http_poster(
    http_client: Any, url: str, secret: str, clock: Any
) -> Callable[[dict], Awaitable[Any]]:
    """POST a body to the ingest; the parsed answer, or :class:`DeliveryError`.

    Only a 200 is an answer. Synapse's shared HTTP client bounds the wait for
    the response headers.
    """
    from synapse.logging.context import make_deferred_yieldable, run_in_background
    from synapse.util.async_helpers import timeout_deferred
    from synapse.util.json import json_encoder
    from twisted.web.client import readBody
    from twisted.web.http_headers import Headers

    headers = Headers(
        {
            b"Content-Type": [b"application/json"],
            b"Authorization": [b"Bearer " + secret.encode("utf-8")],
        }
    )

    async def post(body: dict) -> Any:
        # Synapse's encoder: event content may be an immutabledict.
        data = json_encoder.encode(body).encode("utf-8")

        async def exchange() -> Any:
            response = await http_client.request(
                "POST", url, data=data, headers=headers
            )
            content = await make_deferred_yieldable(readBody(response))
            return response, content

        try:
            response, content = await make_deferred_yieldable(
                timeout_deferred(
                    # run_in_background keeps the logcontext rules across the
                    # timeout wrapper; ensureDeferred lost the context.
                    deferred=run_in_background(exchange),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    clock=clock,
                )
            )
        except Exception as exc:
            raise DeliveryError("http", type(exc).__name__) from exc
        if response.code in (401, 403):
            raise DeliveryError("auth", f"HTTP {response.code}")
        if response.code != 200:
            raise DeliveryError("http", f"HTTP {response.code}")
        try:
            return json.loads(content)
        except ValueError as exc:
            raise DeliveryError("bad_ack", "answer is not JSON") from exc

    return post
