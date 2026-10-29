import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional
from unittest.mock import AsyncMock, Mock, patch

from synapse.api.errors import LimitExceededError

from synapse_pangea_chat.course_session_admins import course_session_admins
from synapse_pangea_chat.course_session_admins.course_session_admins import (
    CourseSessionAdmins,
    admits_course,
    is_activity_session_type,
    is_first_link,
    mute_rule_conditions,
    with_room_listed,
)

ROOM = "!session:example.com"
OTHER = "!other:example.com"
SPACE = "!course:example.com"
TEACHER = "@teacher:example.com"


def _event(
    *,
    event_type: str = "m.space.child",
    state_key: Optional[str] = ROOM,
    content: Optional[Dict[str, Any]] = None,
    unsigned: Optional[Dict[str, Any]] = None,
) -> Any:
    return SimpleNamespace(
        type=event_type,
        state_key=state_key,
        content={"via": ["example.com"]} if content is None else content,
        unsigned=unsigned or {},
        is_state=lambda: state_key is not None,
    )


class TestIsActivitySessionType(unittest.TestCase):
    def test_the_clients_session_type_with_and_without_an_activity(self):
        self.assertTrue(is_activity_session_type("p.activity.session:act-1"))
        self.assertTrue(is_activity_session_type("p.activity.session"))

    def test_other_room_types_are_not_sessions(self):
        self.assertFalse(is_activity_session_type("m.space"))
        self.assertFalse(is_activity_session_type("p.analytics"))
        self.assertFalse(is_activity_session_type("p.activity.sessionx"))
        self.assertFalse(is_activity_session_type(None))
        self.assertFalse(is_activity_session_type(7))


class TestIsFirstLink(unittest.TestCase):
    def test_a_new_link_is_the_first(self):
        self.assertTrue(is_first_link(_event()))

    def test_a_link_sent_again_is_not(self):
        # No backfill: an older session's link re-sent replaces an earlier one.
        event = _event(unsigned={"replaces_state": "$earlier"})
        self.assertFalse(is_first_link(event))

    def test_an_unlink_is_not(self):
        self.assertFalse(is_first_link(_event(content={})))

    def test_other_events_are_not(self):
        self.assertFalse(is_first_link(_event(event_type="m.room.member")))
        self.assertFalse(is_first_link(_event(state_key="")))
        self.assertFalse(is_first_link(_event(state_key=None)))


class TestWithRoomListed(unittest.TestCase):
    def test_adds_to_an_empty_list(self):
        self.assertEqual(with_room_listed(None, ROOM), {"room_ids": [ROOM]})

    def test_keeps_the_list_sorted_and_other_keys(self):
        content = {"room_ids": [ROOM], "other": 1}
        self.assertEqual(
            with_room_listed(content, OTHER),
            {"room_ids": sorted([ROOM, OTHER]), "other": 1},
        )

    def test_keeps_rooms_read_back_frozen(self):
        # Synapse's get_global answers frozen data: the list arrives as a tuple,
        # and every room already on it must stay listed.
        self.assertEqual(
            with_room_listed({"room_ids": (ROOM,)}, OTHER),
            {"room_ids": sorted([ROOM, OTHER])},
        )

    def test_an_already_listed_room_writes_nothing(self):
        self.assertIsNone(with_room_listed({"room_ids": [ROOM]}, ROOM))

    def test_malformed_entries_are_dropped_not_raised_on(self):
        self.assertEqual(
            with_room_listed({"room_ids": [ROOM, 3, None]}, OTHER),
            {"room_ids": sorted([ROOM, OTHER])},
        )
        self.assertEqual(
            with_room_listed({"room_ids": "nope"}, ROOM), {"room_ids": [ROOM]}
        )


class TestMuteRule(unittest.TestCase):
    def test_matches_the_room_like_the_apps_mute(self):
        self.assertEqual(
            mute_rule_conditions(ROOM),
            [{"kind": "event_match", "key": "room_id", "pattern": ROOM}],
        )


class TestAdmitsCourse(unittest.TestCase):
    def _rules(self, join_rule: str, allow_room: Optional[str] = SPACE) -> Any:
        allow = [{"type": "m.room_membership", "room_id": allow_room}]
        return {"join_rule": join_rule, "allow": allow if allow_room else []}

    def test_a_session_restricted_to_this_course_is_admitted(self):
        self.assertTrue(admits_course(self._rules("knock_restricted"), SPACE))
        self.assertTrue(admits_course(self._rules("restricted"), SPACE))

    def test_frozen_allow_lists_are_read(self):
        rules = {
            "join_rule": "knock_restricted",
            "allow": ({"type": "m.room_membership", "room_id": SPACE},),
        }
        self.assertTrue(admits_course(rules, SPACE))

    def test_public_invite_knock_and_other_courses_are_not(self):
        self.assertFalse(admits_course({"join_rule": "public"}, SPACE))
        self.assertFalse(admits_course({"join_rule": "invite"}, SPACE))
        self.assertFalse(admits_course(self._rules("knock"), SPACE))
        self.assertFalse(admits_course(self._rules("public"), SPACE))
        self.assertFalse(admits_course(self._rules("knock_restricted", OTHER), SPACE))
        self.assertFalse(admits_course(self._rules("knock_restricted", None), SPACE))
        self.assertFalse(admits_course(None, SPACE))


def _ev(content: Dict[str, Any], sender: str = TEACHER) -> Any:
    return SimpleNamespace(
        content=content,
        sender=sender,
        room_version=SimpleNamespace(msc4289_creator_power_enabled=False),
    )


class TestEligibilityIsReadBeforeEveryAttempt(unittest.TestCase):
    """An attempt can wait out a rate limit; whatever changed meanwhile is
    honoured on the next attempt."""

    def setUp(self) -> None:
        self.membership: Optional[str] = None
        self.power = 100
        self.linked = True
        self.joins = 0

        async def membership(user_id: str, room_id: str) -> Any:
            return self.membership, None

        async def space_state(room_id: str, event_filter: Any) -> Dict[Any, Any]:
            state: Dict[Any, Any] = {
                ("m.room.create", ""): _ev({"type": "m.space"}),
                ("m.room.power_levels", ""): _ev({"users": {TEACHER: self.power}}),
                ("m.room.member", TEACHER): _ev({"membership": "join"}),
            }
            if self.linked:
                state[("m.space.child", ROOM)] = _ev({"via": ["example.com"]})
            return state

        module: Any = object.__new__(CourseSessionAdmins)
        module._clock = SimpleNamespace(sleep=AsyncMock())
        module._store = Mock(
            get_local_current_membership_for_user_in_room=AsyncMock(
                side_effect=membership
            )
        )
        module._api = Mock(get_room_state=AsyncMock(side_effect=space_state))
        module._list = AsyncMock()
        module._mute = AsyncMock()
        self.module = module

    async def _run(self, during_wait: Callable[[], None]) -> bool:
        async def join(api: Any, room_id: str, user_id: str) -> None:
            self.joins += 1
            if self.joins == 1:
                during_wait()
                raise LimitExceededError("test", retry_after_ms=1)

        with patch.object(course_session_admins, "server_initiated_join", join):
            return await self.module._join_admin(SPACE, ROOM, TEACHER)

    def _assert_skipped(self, during_wait: Callable[[], None]) -> None:
        self.assertFalse(asyncio.run(self._run(during_wait)))
        self.assertEqual(self.joins, 1)
        self.module._mute.assert_not_awaited()

    def test_an_admin_who_left_during_the_wait_is_not_rejoined(self):
        self._assert_skipped(lambda: setattr(self, "membership", "leave"))

    def test_an_admin_banned_during_the_wait_is_not_joined(self):
        self._assert_skipped(lambda: setattr(self, "membership", "ban"))

    def test_an_admin_demoted_during_the_wait_is_not_joined(self):
        self._assert_skipped(lambda: setattr(self, "power", 50))

    def test_a_session_unlinked_during_the_wait_is_not_joined(self):
        self._assert_skipped(lambda: setattr(self, "linked", False))

    def test_with_nothing_changed_the_next_attempt_joins_and_mutes(self):
        self.assertTrue(asyncio.run(self._run(lambda: None)))
        self.assertEqual(self.joins, 2)
        self.module._mute.assert_awaited_once_with(TEACHER, ROOM)


class TestBackfillCourse(unittest.TestCase):
    def _module(self, join_admins: Any) -> Any:
        module: Any = object.__new__(CourseSessionAdmins)
        module.join_admins = AsyncMock(side_effect=join_admins)
        return module

    def test_counts_joins_and_one_failing_room_does_not_stop_the_rest(self):
        async def join_admins(space_id: str, session_id: str) -> Any:
            if session_id == OTHER:
                raise RuntimeError("boom")
            return [TEACHER]

        module = self._module(join_admins)
        rooms = [ROOM, OTHER, "!third:example.com"]
        self.assertEqual(asyncio.run(module.backfill_course(SPACE, rooms)), 2)
        self.assertEqual(module.join_admins.await_count, 3)

    def test_a_course_with_nothing_left_to_do_makes_no_joins(self):
        module = self._module(lambda space_id, session_id: [])
        self.assertEqual(asyncio.run(module.backfill_course(SPACE, [ROOM])), 0)


class TestCourseSessions(unittest.TestCase):
    def test_only_live_links_to_course_sessions_are_walked(self):
        linked = {
            ("m.space.child", ROOM): _ev({"via": ["example.com"]}),
            ("m.space.child", OTHER): _ev({"via": ["example.com"]}),
            ("m.space.child", "!gone:example.com"): _ev({}),
        }
        module: Any = object.__new__(CourseSessionAdmins)
        module._api = Mock(get_room_state=AsyncMock(return_value=linked))

        async def refusal(space_id: str, room_id: str) -> Optional[str]:
            return None if room_id == ROOM else "not an activity session"

        module._not_a_course_session = AsyncMock(side_effect=refusal)
        self.assertEqual(asyncio.run(module.course_sessions(SPACE)), [ROOM])
        checked = {c.args[1] for c in module._not_a_course_session.await_args_list}
        self.assertNotIn("!gone:example.com", checked)


def _module_on(instance: str, writer: str = "master") -> Any:
    """A module built as Synapse builds it, on ``instance`` of a homeserver
    whose push rules writer is ``writer``."""
    hs = Mock()
    hs.get_instance_name.return_value = instance
    hs.config.worker.writers.push_rules = [writer]
    return CourseSessionAdmins(Mock(_hs=hs))


class TestOnlyThePushRulesWriterJoins(unittest.TestCase):
    """The hook fires on every Synapse process, but a push rule can only be
    written on the push rules writer: that one process does all the work."""

    def _link_session(self, instance: str, writer: str) -> Any:
        module = _module_on(instance, writer)
        module._not_a_course_session = AsyncMock(return_value=None)
        module._course_admins = AsyncMock(return_value=frozenset({TEACHER}))
        module._still_eligible = AsyncMock(return_value=True)
        module._list = AsyncMock()
        module._mute = AsyncMock()
        join = AsyncMock()
        scheduled: list = []
        module._clock.call_later = Mock(
            side_effect=lambda _delay, f, *args: scheduled.append((f, args))
        )
        module._api.run_as_background_process = lambda _desc, func, *args: func(*args)

        async def run() -> None:
            await module.on_new_event(
                SimpleNamespace(**vars(_event()), room_id=SPACE, event_id="$e"), {}
            )
            for f, args in scheduled:
                await f(*args)

        with patch.object(course_session_admins, "server_initiated_join", join):
            asyncio.run(run())
        module.join = join
        return module

    def test_another_process_does_no_join_list_or_mute(self):
        for instance, writer in (("generic-0", "master"), ("master", "pushw")):
            with self.subTest(instance=instance, writer=writer):
                module = self._link_session(instance, writer)
                module._course_admins.assert_not_awaited()
                module._list.assert_not_awaited()
                module.join.assert_not_awaited()
                module._mute.assert_not_awaited()

    def test_the_push_rules_writer_lists_joins_and_mutes(self):
        for writer in ("master", "pushw"):
            with self.subTest(writer=writer):
                module = self._link_session(writer, writer)
                module._list.assert_awaited_once_with(TEACHER, ROOM)
                module.join.assert_awaited_once()
                self.assertEqual(module.join.await_args.args[1:], (ROOM, TEACHER))
                module._mute.assert_awaited_once_with(TEACHER, ROOM)


class TestBackfillRunsOnlyOnThePushRulesWriter(unittest.TestCase):
    def _post(self, instance: str, writer: str) -> Any:
        module = _module_on(instance, writer)
        module.is_space = AsyncMock(return_value=True)
        module.course_sessions = AsyncMock(return_value=[ROOM])
        api = Mock(is_user_admin=AsyncMock(return_value=True))
        resource = course_session_admins.BackfillCourseSessionAdmins(api, module)
        resource._auth = Mock(get_user_by_req=AsyncMock(return_value=Mock()))
        respond = Mock()
        with patch.object(
            course_session_admins,
            "extract_body_json",
            AsyncMock(return_value={"room_id": SPACE}),
        ), patch.object(course_session_admins, "respond_with_json", respond):
            asyncio.run(resource._async_render_POST(Mock()))
        return SimpleNamespace(api=api, module=module, code=respond.call_args.args[1])

    def test_another_process_refuses_and_schedules_nothing(self):
        for instance, writer in (("generic-0", "master"), ("master", "pushw")):
            with self.subTest(instance=instance, writer=writer):
                posted = self._post(instance, writer)
                self.assertEqual(posted.code, 421)
                posted.module.course_sessions.assert_not_awaited()
                posted.api.run_as_background_process.assert_not_called()

    def test_the_push_rules_writer_schedules_the_backfill(self):
        for writer in ("master", "pushw"):
            with self.subTest(writer=writer):
                posted = self._post(writer, writer)
                self.assertEqual(posted.code, 202)
                posted.api.run_as_background_process.assert_called_once()


if __name__ == "__main__":
    unittest.main()
