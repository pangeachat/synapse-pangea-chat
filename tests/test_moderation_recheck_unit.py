"""Encryption is refused while moderation is on, and a message Tier 2 could
not check is checked again (pangeachat/.github#870)."""

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, cast
from unittest.mock import AsyncMock, patch

from synapse.api.errors import Codes, SynapseError
from synapse.events import EventBase
from synapse.module_api import NOT_SPAM

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import ChatModeration
from synapse_pangea_chat.moderation.choreo_client import BatchVerdicts
from synapse_pangea_chat.moderation.dispatch import ModerationJob
from synapse_pangea_chat.moderation.recheck import MAX_AGE_SECONDS

from .moderation_doubles import EventStoreDouble, HomeServerDouble, MetricReader
from .moderation_doubles import module_api as module_api_double

ROOM = "!room:example.org"
REFUSED = "pangea_moderation_encryption_refused_total"
RECHECK = "pangea_moderation_tier2_recheck_total"
FLAGGED = {"flagged": True, "categories": ["harassment"], "evaluated": True}
CLEAN = {"flagged": False, "categories": [], "evaluated": True}
UNUSABLE = {"flagged": True, "categories": [], "evaluated": True}


def _config(tier1: bool = False, tier2: bool = True) -> PangeaChatConfig:
    return PangeaChatConfig(
        cms_base_url="http://cms.invalid",
        cms_service_api_key="k",
        moderation_tier1_enabled=tier1,
        moderation_tier2_enabled=tier2,
        moderation_choreo_base_url="http://choreo.invalid",
        moderation_choreo_access_token="syt_test",
        moderation_exempt_user_id_globs=["@bot:example.org"],
    )


class _Event:
    def __init__(
        self,
        event_type: str = "m.room.message",
        body: str = "you suck",
        event_id: str = "$e1",
        sender: str = "@learner:example.org",
        redacted: bool = False,
    ) -> None:
        self.type = event_type
        self.event_id = event_id
        self.room_id = ROOM
        self.sender = sender
        self.content: Dict[str, Any] = (
            {"msgtype": "m.text", "body": body}
            if event_type == "m.room.message"
            else {}
        )
        self.internal_metadata = SimpleNamespace(
            is_redacted=lambda: redacted, stream_ordering=7
        )


def _event(*args: Any, **kwargs: Any) -> EventBase:
    return cast(EventBase, _Event(*args, **kwargs))


class _Store(EventStoreDouble):
    """The event store, holding real-shaped events by id."""

    def __init__(self) -> None:
        super().__init__()
        self.events: Dict[str, Any] = {}
        self.broken: List[str] = []

    async def get_event(self, event_id: str, allow_none: bool = False) -> Any:
        self.reads.append(event_id)
        if event_id in self.broken:
            raise RuntimeError("database fell over")
        return self.events.get(event_id)


def _job(event_id: str = "$e1") -> ModerationJob:
    return ModerationJob(
        event_id=event_id,
        room_id=ROOM,
        sender="@learner:example.org",
        text="you suck",
        enqueued_at=0.0,
    )


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = _Store()
        self.homeserver = HomeServerDouble(self.store)
        self.api = module_api_double(self.homeserver)
        self.metrics = MetricReader()

    def _module(self, **kwargs: Any) -> ChatModeration:
        mod = ChatModeration(self.api, _config(**kwargs))
        self.addCleanup(self._stop, mod)
        return mod

    @staticmethod
    def _stop(mod: ChatModeration) -> None:
        if mod._dispatcher is not None:
            mod._dispatcher._stopping = True
            mod._dispatcher._wake_all()

    def _rows(self) -> List[Tuple[Any, ...]]:
        try:
            return list(
                self.store.db_pool.connection.execute(
                    "SELECT event_id, room_id, attempts FROM pangea_moderation_recheck"
                    " ORDER BY event_id"
                )
            )
        except Exception as exc:  # the table is created on first write
            if "no such table" in str(exc):
                return []
            raise

    def _sent(self) -> AsyncMock:
        return cast(AsyncMock, self.api.create_and_send_event_into_room)

    def _check(self, mod: ChatModeration, *results: Optional[Dict[str, Any]]) -> Any:
        assert mod._checker is not None
        return patch.object(mod._checker, "check", AsyncMock(side_effect=results))


class TestEncryptionIsRefused(_Base):
    async def test_create_room_with_encryption_in_initial_state_is_refused(
        self,
    ) -> None:
        for tiers in ({"tier1": True, "tier2": False}, {"tier1": False}):
            with self.subTest(tiers=tiers):
                mod = self._module(**tiers)
                self.metrics.snapshot(REFUSED, hook="create_room")
                with self.assertRaises(SynapseError) as raised:
                    await mod.on_create_room(
                        SimpleNamespace(),
                        {
                            "initial_state": [
                                {"type": "m.room.topic", "content": {}},
                                {
                                    "type": "m.room.encryption",
                                    "state_key": "",
                                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                                },
                            ]
                        },
                        False,
                    )
                self.assertEqual(raised.exception.code, 403)
                self.assertEqual(raised.exception.errcode, Codes.FORBIDDEN)
                self.assertEqual(self.metrics.delta(REFUSED, hook="create_room"), 1)

    async def test_create_room_without_encryption_is_allowed(self) -> None:
        mod = self._module()
        contents: List[Dict[str, Any]] = [
            {},
            {"initial_state": [{"type": "m.room.topic", "content": {}}]},
            {"initial_state": "not a list"},
            {"initial_state": ["not a dict"]},
        ]
        for content in contents:
            with self.subTest(content=content):
                await mod.on_create_room(SimpleNamespace(), content, True)

    async def test_check_event_allowed_refuses_both_encryption_types(self) -> None:
        mod = self._module()
        for event_type in ("m.room.encryption", "m.room.encrypted"):
            with self.subTest(event_type=event_type):
                self.metrics.snapshot(REFUSED, hook="event_allowed")
                allowed, _ = await mod.check_event_allowed(
                    _event(event_type, sender="@bot:example.org"), {}
                )
                self.assertFalse(allowed)
                self.assertEqual(self.metrics.delta(REFUSED, hook="event_allowed"), 1)
        self.assertEqual(
            await mod.check_event_allowed(_event("m.room.message"), {}), (True, None)
        )

    async def test_spam_check_refuses_both_encryption_types_for_every_sender(
        self,
    ) -> None:
        mod = self._module()
        for event_type in ("m.room.encryption", "m.room.encrypted"):
            for sender in ("@learner:example.org", "@bot:example.org"):
                with self.subTest(event_type=event_type, sender=sender):
                    self.metrics.snapshot(REFUSED, hook="spam_check")
                    result = await mod.refuse_encrypted_event(
                        _event(event_type, sender=sender)
                    )
                    assert isinstance(result, tuple)
                    self.assertEqual(result[0], Codes.FORBIDDEN)
                    self.assertTrue(result[1]["error"])
                    self.assertEqual(self.metrics.delta(REFUSED, hook="spam_check"), 1)
        self.assertEqual(
            await mod.refuse_encrypted_event(_event("m.room.message")), NOT_SPAM
        )

    def test_every_hook_is_registered_whichever_tier_is_on(self) -> None:
        for tiers in ({"tier1": True, "tier2": False}, {"tier1": False}):
            with self.subTest(tiers=tiers):
                api = module_api_double(HomeServerDouble())
                mod = ChatModeration(api, _config(**tiers))
                self.addCleanup(self._stop, mod)
                cast(Any, api.register_third_party_rules_callbacks).assert_any_call(
                    check_event_allowed=mod.check_event_allowed,
                    on_create_room=mod.on_create_room,
                )
                cast(Any, api.register_spam_checker_callbacks).assert_any_call(
                    check_event_for_spam=mod.refuse_encrypted_event
                )


class TestAnUncheckedMessageIsQueued(_Base):
    async def test_no_verdict_or_unusable_verdict_queues_the_message(self) -> None:
        mod = self._module()
        with self._check(mod, None, UNUSABLE):
            await mod._screen_batch((_job("$a"),))
            await mod._screen_batch((_job("$b"),))
        self.assertEqual(self._rows(), [("$a", ROOM, 0), ("$b", ROOM, 0)])
        self._sent().assert_not_awaited()

    async def test_a_verdict_queues_nothing(self) -> None:
        mod = self._module()
        with self._check(mod, CLEAN, FLAGGED):
            await mod._screen_batch((_job("$a"),))
            self.store.events["$b"] = _Event(event_id="$b")
            await mod._screen_batch((_job("$b"),))
        self.assertEqual(self._rows(), [])
        self._sent().assert_awaited_once()

    async def test_a_handler_error_queues_the_message_and_still_raises(self) -> None:
        mod = self._module()
        assert mod._checker is not None
        with patch.object(
            mod._checker, "check", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            with self.assertRaises(RuntimeError):
                await mod._screen_batch((_job("$a"),))
        self.assertEqual(self._rows(), [("$a", ROOM, 0)])

    async def test_a_failed_batch_queues_every_message(self) -> None:
        mod = self._module()
        assert mod._checker is not None
        with patch.object(
            mod._checker, "check_batch", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            with self.assertRaises(RuntimeError):
                await mod._screen_batch((_job("$a"), _job("$b")))
        self.assertEqual(self._rows(), [("$a", ROOM, 0), ("$b", ROOM, 0)])

    async def test_a_batch_entry_with_no_verdict_is_queued(self) -> None:
        mod = self._module()
        assert mod._checker is not None
        verdicts = BatchVerdicts(results=[CLEAN, None], confirmed=False)
        with patch.object(
            mod._checker, "check_batch", AsyncMock(return_value=verdicts)
        ):
            await mod._screen_batch((_job("$a"), _job("$b")))
        self.assertEqual(self._rows(), [("$b", ROOM, 0)])

    async def test_a_confirmed_batch_entry_with_no_verdict_is_queued(self) -> None:
        mod = self._module()
        assert mod._checker is not None
        verdicts = BatchVerdicts(results=[UNUSABLE, CLEAN], confirmed=True)
        with patch.object(
            mod._checker, "check_batch", AsyncMock(return_value=verdicts)
        ):
            await mod._screen_batch((_job("$a"), _job("$b")))
        self.assertEqual(self._rows(), [("$a", ROOM, 0)])

    async def test_a_decision_error_in_a_batch_queues_that_message(self) -> None:
        mod = self._module()
        assert mod._checker is not None
        verdicts = BatchVerdicts(results=[FLAGGED, CLEAN], confirmed=False)
        with patch.object(
            mod._checker, "check_batch", AsyncMock(return_value=verdicts)
        ), patch.object(
            mod._checker, "check", AsyncMock(side_effect=RuntimeError("boom"))
        ), self.assertLogs(
            "synapse.modules.synapse_pangea_chat", "WARNING"
        ):
            await mod._screen_batch((_job("$a"), _job("$b")))
        self.assertEqual(self._rows(), [("$a", ROOM, 0)])

    async def test_a_screen_flag_whose_confirmation_fails_is_queued(self) -> None:
        mod = self._module()
        assert mod._checker is not None
        verdicts = BatchVerdicts(results=[FLAGGED, CLEAN], confirmed=False)
        with patch.object(
            mod._checker, "check_batch", AsyncMock(return_value=verdicts)
        ), self._check(mod, None):
            await mod._screen_batch((_job("$a"), _job("$b")))
        self.assertEqual(self._rows(), [("$a", ROOM, 0)])

    async def test_a_message_dropped_by_a_full_queue_is_queued(self) -> None:
        mod = self._module()
        assert mod._dispatcher is not None
        mod._dispatcher._queue_size = 1
        self.metrics.snapshot(
            "pangea_moderation_tier2_dropped_total", cause="queue_full"
        )
        await mod.on_new_event(_event(event_id="$a"), {})
        await mod.on_new_event(_event(event_id="$b"), {})
        self.assertEqual(
            self.metrics.delta(
                "pangea_moderation_tier2_dropped_total", cause="queue_full"
            ),
            1,
        )
        self.assertEqual(self._rows(), [("$b", ROOM, 0)])

    async def test_queueing_twice_keeps_one_row(self) -> None:
        mod = self._module()
        with self._check(mod, None, None):
            await mod._screen_batch((_job("$a"),))
            await mod._screen_batch((_job("$a"),))
        self.assertEqual(self._rows(), [("$a", ROOM, 0)])

    async def test_a_failed_write_is_counted_lost_and_does_not_raise(self) -> None:
        mod = self._module()
        self.store.db_pool.error = RuntimeError("database down")
        self.metrics.snapshot(RECHECK, outcome="lost")
        with self._check(mod, None):
            await mod._screen_batch((_job("$a"),))
        self.assertEqual(self.metrics.delta(RECHECK, outcome="lost"), 1)


class TestTheRecheckPass(_Base):
    async def _queue(self, mod: ChatModeration, *event_ids: str) -> None:
        with self._check(mod, *([None] * len(event_ids))):
            for event_id in event_ids:
                await mod._screen_batch((_job(event_id),))

    def test_the_pass_runs_every_30_seconds_on_the_tier2_instance_only(self) -> None:
        for active in (True, False):
            with self.subTest(active=active):
                homeserver = HomeServerDouble()
                calls: List[Tuple[Any, ...]] = []
                looping_call = homeserver.clock.looping_call

                def _record(f: Any, interval: Any, *args: Any) -> Any:
                    calls.append((interval, *args))
                    return looping_call(f, interval, *args)

                with patch.object(homeserver.clock, "looping_call", _record):
                    mod = ChatModeration(
                        module_api_double(homeserver, run_background_tasks=active),
                        _config(),
                    )
                self.addCleanup(self._stop, mod)
                rechecks = [call for call in calls if mod._recheck_pass in call[1:]]
                self.assertEqual(
                    [int(call[0]) for call in rechecks], [30000] if active else []
                )

    async def test_a_verdict_redacts_through_the_normal_path_and_drops_the_row(
        self,
    ) -> None:
        mod = self._module()
        await self._queue(mod, "$a")
        self.store.events["$a"] = _Event(event_id="$a")
        self.metrics.snapshot(RECHECK, outcome="decided")
        with self._check(mod, FLAGGED) as check:
            await mod._recheck_pass()
        check.assert_awaited_once_with("you suck")
        sent = self._sent()
        sent.assert_awaited_once()
        assert sent.await_args is not None
        self.assertEqual(sent.await_args.args[0]["redacts"], "$a")
        self.assertEqual(self._rows(), [])
        self.assertEqual(self.metrics.delta(RECHECK, outcome="decided"), 1)

    async def test_still_no_verdict_keeps_the_row_and_writes_no_incident(
        self,
    ) -> None:
        mod = self._module()
        await self._queue(mod, "$a")
        self.store.events["$a"] = _Event(event_id="$a")
        with self._check(mod, None, UNUSABLE), patch.object(
            mod._recorder, "record_verdict", AsyncMock()
        ) as record:
            await mod._recheck_pass()
            await mod._recheck_pass()
        record.assert_not_awaited()
        self._sent().assert_not_awaited()
        self.assertEqual(self._rows(), [("$a", ROOM, 2)])

    async def test_a_check_that_raises_counts_as_an_attempt(self) -> None:
        mod = self._module()
        await self._queue(mod, "$a")
        self.store.events["$a"] = _Event(event_id="$a")
        assert mod._checker is not None
        with patch.object(
            mod._checker, "check", AsyncMock(side_effect=RuntimeError("boom"))
        ), self.assertLogs("synapse.modules.synapse_pangea_chat", "WARNING"):
            await mod._recheck_pass()
        self.assertEqual(self._rows(), [("$a", ROOM, 1)])

    async def test_rows_behind_the_first_batch_still_get_their_turn(self) -> None:
        mod = self._module()
        ids = [f"$e{n:02d}" for n in range(26)]
        await self._queue(mod, *ids)
        for event_id in ids:
            self.store.events[event_id] = _Event(event_id=event_id)
        with self._check(mod, *([UNUSABLE] * 50)):
            await mod._recheck_pass()
            await mod._recheck_pass()
        attempts = {row[0]: row[2] for row in self._rows()}
        self.assertGreaterEqual(attempts["$e25"], 1, "row 26 was never retried")

    async def test_a_gone_or_redacted_message_is_dropped_unchecked(self) -> None:
        mod = self._module()
        await self._queue(mod, "$gone", "$redacted")
        self.store.events["$redacted"] = _Event(event_id="$redacted", redacted=True)
        with self._check(mod) as check:
            await mod._recheck_pass()
        check.assert_not_awaited()
        self.assertEqual(self._rows(), [])

    async def test_an_expired_row_is_dropped_and_counted(self) -> None:
        mod = self._module()
        await self._queue(mod, "$old")
        self.store.events["$old"] = _Event(event_id="$old")
        self.homeserver.clock.now += MAX_AGE_SECONDS + 1
        self.metrics.snapshot(RECHECK, outcome="expired")
        with self._check(mod) as check, self.assertLogs(
            "synapse.modules.synapse_pangea_chat", "WARNING"
        ):
            await mod._recheck_pass()
        check.assert_not_awaited()
        self.assertEqual(self._rows(), [])
        self.assertEqual(self.metrics.delta(RECHECK, outcome="expired"), 1)

    async def test_one_bad_row_does_not_block_the_others(self) -> None:
        mod = self._module()
        await self._queue(mod, "$a", "$b")
        self.store.broken.append("$a")
        self.store.events["$b"] = _Event(event_id="$b")
        with self._check(mod, CLEAN), self.assertLogs(
            "synapse.modules.synapse_pangea_chat", "WARNING"
        ):
            await mod._recheck_pass()
        self.assertEqual(self._rows(), [("$a", ROOM, 1)])

    async def test_an_open_breaker_skips_the_pass(self) -> None:
        mod = self._module()
        await self._queue(mod, "$a")
        self.store.events["$a"] = _Event(event_id="$a")
        assert mod._breaker is not None
        for _ in range(50):
            mod._breaker.record_failure(mod._breaker.check()[1])
        with self._check(mod, CLEAN) as check:
            await mod._recheck_pass()
        check.assert_not_awaited()
        self.assertEqual(self._rows(), [("$a", ROOM, 0)])
