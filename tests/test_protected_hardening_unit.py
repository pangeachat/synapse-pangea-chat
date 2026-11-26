"""Protected courses hardening (pangeachat/.github#799): calls both ways in a
course room of two, restoring memberships room by room and unrate-limited, an
older request granted after protection asked rather than managed, the setup
of a Protected course repeatable, the one-way rule and the controls write
serialized, a graduation's effects repeatable, two graduations' power levels
serialized, and the graduation write conditional on the record still being
managed.

The concurrency tests interleave for real: the first call is held between
its read and its write while the second runs. Synapse's ``Linearizer`` waits
on Twisted deferreds, which an asyncio test loop cannot drive, so these tests
give the code an asyncio lock with the same ``queue(key)`` shape.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Tuple, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.api.errors import Codes

from synapse_pangea_chat.blocked_join_gate.blocked_join_gate import (
    server_initiated_join,
)
from synapse_pangea_chat.joined_via import REMOVED_BY, module_write
from synapse_pangea_chat.student_invitations.controls import Controls
from synapse_pangea_chat.student_invitations.graduation import Graduation
from synapse_pangea_chat.student_invitations.protected import (
    COURSE_MANAGEMENT,
    ProtectedRules,
    protected_since,
)
from synapse_pangea_chat.student_invitations.protected_api import (
    ProtectedCourseHandlers,
)
from tests.test_protected_courses_unit import (
    LATER,
    FakeApi,
    FakeControls,
    _Base,
    event,
    member,
    state,
)
from tests.test_protected_graduation_unit import FakeGraduation
from tests.test_seat_invitations_unit import (
    OTHER,
    ROOM,
    STUDENT,
    TEACHER,
    THIRD,
)

CHAT_COURSES = "synapse_pangea_chat.student_invitations.controls.ChatCourses"
CONTROLS_REPORT = "synapse_pangea_chat.student_invitations.controls.report_failure"
CHAT = "!pair:x"


class AsyncioLocks:
    """``Linearizer.queue(key)`` over asyncio locks, for a test loop."""

    def __init__(self) -> None:
        self._locks: Dict[Any, asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def queue(self, key: Any) -> AsyncIterator[None]:
        async with self._locks.setdefault(key, asyncio.Lock()):
            yield


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# --- 1. calls in a course room of two apply both ways ---


class _PairApi:
    """A course room of two, ``CHAT``, owned by the Protected course ROOM,
    which manages STUDENT with in-course calls off."""

    def __init__(self, members: Tuple[str, str]) -> None:
        self.members = members
        self.main = SimpleNamespace(
            get_users_in_room=self._joined,
            get_local_current_membership_for_user_in_room=self._membership,
            get_event=AsyncMock(return_value=None),
        )
        self._hs = SimpleNamespace(
            get_datastores=lambda: SimpleNamespace(main=self.main),
            get_storage_controllers=lambda: None,
        )

    async def _joined(self, room_id: str) -> List[str]:
        return list(self.members)

    async def _membership(self, user_id: str, room_id: str) -> Tuple[str, None]:
        return "join", None

    async def get_room_state(self, room_id: str, keys: Any) -> Dict[Any, Any]:
        types = {t for t, _ in keys}
        if COURSE_MANAGEMENT in types:
            content = {"protected": True, "in_course": {"calls": False}}
            return {(COURSE_MANAGEMENT, ""): SimpleNamespace(content=content)}
        return {
            ("m.room.member", u): SimpleNamespace(content={"membership": "join"})
            for u in self.members
        }


class _PairStore:
    async def managed_rows(self) -> List[Tuple[str, str]]:
        return [(STUDENT, ROOM)]

    async def managing_courses(self, user_ids: Any) -> Dict[str, List[str]]:
        return {STUDENT: [ROOM]} if STUDENT in user_ids else {}


class _PairAdmins:
    async def is_course_admin(self, room_id: str, user_id: str) -> bool:
        return user_id == TEACHER


class _Owned:
    async def for_room(self, room_id: str, position: int) -> Tuple[str, ...]:
        return (ROOM,) if room_id == CHAT else ()

    def position_now(self) -> int:
        return 0


def pair_controls(members: Tuple[str, str]) -> Controls:
    with patch(CHAT_COURSES):
        controls = Controls(_PairApi(members), _PairStore(), _PairAdmins(), None, ())
    controls._courses = cast(Any, _Owned())
    return controls


def send(sender: str, type: str = "m.call.invite") -> Any:
    return SimpleNamespace(sender=sender, room_id=CHAT, type=type, redacts=None)


CALLS_OFF = (Codes.FORBIDDEN, {"org.pangea.control": "in_course.calls"})


class TestCallsBothWaysInACourseRoomOfTwo(IsolatedAsyncioTestCase):
    async def test_a_classmates_call_to_the_student_is_refused(self) -> None:
        controls = pair_controls((STUDENT, OTHER))
        self.assertEqual(await controls.check_event_for_spam(send(OTHER)), CALLS_OFF)
        self.assertEqual(await controls.check_event_for_spam(send(STUDENT)), CALLS_OFF)
        # Only calls: a message in the course room is the room's to allow.
        message = send(OTHER, "m.room.message")
        self.assertEqual(await controls.check_event_for_spam(message), "NOT_SPAM")

    async def test_the_courses_admin_can_always_call_and_be_called(self) -> None:
        controls = pair_controls((STUDENT, TEACHER))
        self.assertEqual(await controls.check_event_for_spam(send(TEACHER)), "NOT_SPAM")
        self.assertEqual(await controls.check_event_for_spam(send(STUDENT)), "NOT_SPAM")


# --- 2. restoring memberships: room by room, never rate limited ---


class _RestoreJoiner:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    async def force_join(self, room_id: str, user_id: str, **kw: Any) -> Dict:
        self.calls.append((room_id, kw))
        if room_id == "!a:x":
            raise RuntimeError("M_LIMIT_EXCEEDED")
        return {"success": room_id != "!b:x"}


class TestRestoreIsolatedPerRoom(IsolatedAsyncioTestCase):
    async def test_one_rooms_failure_does_not_block_the_rest(self) -> None:
        main = SimpleNamespace(
            get_rooms_for_user=AsyncMock(return_value=set()),
            get_rooms_for_local_user_where_membership_is=AsyncMock(
                return_value=[
                    SimpleNamespace(room_id=r, event_id=f"${r}")
                    for r in ("!a:x", "!b:x", "!c:x")
                ]
            ),
            get_event=AsyncMock(
                return_value=SimpleNamespace(content={REMOVED_BY: "x"})
            ),
        )
        api = SimpleNamespace(
            _hs=SimpleNamespace(
                get_datastores=lambda: SimpleNamespace(main=main),
                get_storage_controllers=lambda: None,
            )
        )
        joiner = _RestoreJoiner()
        with patch(CHAT_COURSES):
            controls = Controls(api, _PairStore(), _PairAdmins(), joiner, ())
        controls._courses = cast(Any, _Owned())
        with patch(CONTROLS_REPORT) as report:
            await controls.reconcile(OTHER)  # not managed: every room restored
        self.assertEqual([r for r, _ in joiner.calls], ["!a:x", "!b:x", "!c:x"])
        # A restore is the module's own repair, not the student's request.
        self.assertTrue(all(kw.get("ratelimit") is False for _, kw in joiner.calls))
        self.assertEqual(report.call_count, 2)

    async def test_an_unrate_limited_join_goes_through_the_member_handler(
        self,
    ) -> None:
        api = MagicMock()
        handler = api._hs.get_room_member_handler.return_value
        handler.update_membership = AsyncMock(return_value=("$j", 1))
        api.update_room_membership = AsyncMock()
        await server_initiated_join(api, ROOM, STUDENT, via=None, ratelimit=False)
        api.update_room_membership.assert_not_called()
        kw = handler.update_membership.call_args.kwargs
        self.assertEqual((kw["action"], kw["ratelimit"]), ("join", False))
        self.assertEqual(kw["target"].to_string(), STUDENT)
        await server_initiated_join(api, ROOM, STUDENT, via=None)
        api.update_room_membership.assert_awaited_once()


# --- 3. a request made before protection, granted after it, is asked ---


class TestAnOlderRequestGrantedAfterProtection(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()

        async def get_user_by_id(user_id: str) -> Any:
            return SimpleNamespace(creation_ts=1)  # older than any invitation

        self.h.main.get_user_by_id = get_user_by_id
        (inv,) = await self.h.add("someone@else.example")
        self.ident = inv["invitation_id"]

    async def test_it_is_asked_not_managed(self) -> None:
        # Not Protected yet: the request is recorded with no question.
        status, body = await self.h.handlers.open(STUDENT, self.ident)
        self.assertEqual(body["result"], "pending")
        self.h.admins.protected.add(ROOM)
        self.h.admins.since[ROOM] = LATER
        outcome, _ = await self.h.approvals.grant(self.ident, STUDENT, TEACHER)
        self.assertEqual(outcome, "claimed")
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual(record["status"], "asked")
        self.assertEqual(
            (await self.ledger())[-1][:3], (STUDENT, TEACHER, "management_asked")
        )
        # Their answer is the existing one.
        self.assertEqual(
            await self.store.answer(
                ROOM, STUDENT, accept=True, actor=STUDENT, now_ms=LATER
            ),
            "managed",
        )

    async def test_a_request_made_under_protection_was_asked_then(self) -> None:
        self.h.admins.protected.add(ROOM)
        self.h.admins.since[ROOM] = 1
        status, body = await self.h.handlers.open(STUDENT, self.ident, consent=True)
        self.assertEqual(body["result"], "pending")
        outcome, _ = await self.h.approvals.grant(self.ident, STUDENT, TEACHER)
        self.assertEqual(outcome, "claimed")
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual(record["status"], "managed")


# --- 4. the setup of a Protected course completes on a retry ---


class TestProtectedSince(_Base):
    async def test_the_oldest_of_the_unbroken_protected_run(self) -> None:
        first = event(COURSE_MANAGEMENT, {"protected": False}, event_id="$1", ts=10)
        on = event(
            COURSE_MANAGEMENT, {"protected": True}, event_id="$2", ts=20, replaces="$1"
        )
        later = event(
            COURSE_MANAGEMENT,
            {"protected": True, "outside": {"dms": True}},
            event_id="$3",
            ts=30,
            replaces="$2",
        )
        self.events.add(first, on, later)
        self.assertEqual(await protected_since(self.h.main, later), 20)
        self.assertIsNone(await protected_since(self.h.main, first))
        self.assertIsNone(await protected_since(self.h.main, None))


class _RouteBase(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.fake = FakeApi(self.h)
        self.controls = FakeControls()
        self.graduation = FakeGraduation()
        self.p = ProtectedCourseHandlers(
            self.fake,
            self.store,
            self.h.admins,
            self.h.accounts,
            self.h.claims.unmanaged,
            True,
            cast(Controls, self.controls),
            cast(Any, self.graduation),
        )
        self.p._locks = cast(Any, AsyncioLocks())


class TestSetupRepeatable(_RouteBase):
    async def test_a_retry_after_a_failed_ask_asks_the_members(self) -> None:
        # Managed by a claim before the course was Protected: to be asked.
        await self.store.start_managed(
            ROOM, STUDENT, actor="system", via="email", joined_at_ms=1, now_ms=1
        )
        real_ask = self.store.ask
        failures = [RuntimeError("db")]

        async def ask_failing_once(*args: Any) -> List[str]:
            if failures:
                raise failures.pop()
            return await real_ask(*args)

        with patch.object(self.store, "ask", ask_failing_once):
            with self.assertRaises(RuntimeError):
                await self.p.set(TEACHER, {"room_id": ROOM, "protected": True})
            status, body = await self.p.set(
                TEACHER, {"room_id": ROOM, "protected": True}
            )
        self.assertEqual((status, body["asked"]), (200, [STUDENT]))
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual(record["status"], "asked")

    async def test_a_later_change_asks_no_one_managed_since(self) -> None:
        await self.p.set(TEACHER, {"room_id": ROOM, "protected": True})
        # A join after protection, managed by the hook; and the asked member's yes.
        self.fake.room_state[("m.room.member", THIRD)] = member(THIRD, "join")
        await self.store.start_managed(
            ROOM, THIRD, actor="system", via="code", joined_at_ms=200, now_ms=200
        )
        await self.store.answer(ROOM, STUDENT, accept=True, actor=STUDENT, now_ms=300)
        status, body = await self.p.set(
            TEACHER, {"room_id": ROOM, "outside": {"dms": True}}
        )
        self.assertEqual((status, body["asked"]), (200, []))


# --- 5. protection is one-way, and a controls write is serialized ---


class TestOneWayAndSerialized(_RouteBase):
    async def test_overlapping_updates_never_turn_protection_off(self) -> None:
        gate = asyncio.Event()
        held: List[bool] = []
        read = self.fake.get_room_state

        async def hold_first_read(room_id: str, event_filter: Any) -> Dict:
            result = await read(room_id, event_filter)
            if not held and any(t == COURSE_MANAGEMENT for t, _ in event_filter):
                held.append(True)
                await gate.wait()
            return result

        with patch.object(self.fake, "get_room_state", hold_first_read):
            controls_only = asyncio.ensure_future(
                self.p.set(TEACHER, {"room_id": ROOM, "in_course": {"calls": False}})
            )
            await settle()
            turn_on = asyncio.ensure_future(
                self.p.set(TEACHER, {"room_id": ROOM, "protected": True})
            )
            await settle()
            gate.set()
            await asyncio.gather(controls_only, turn_on)
        current = self.fake.room_state[(COURSE_MANAGEMENT, "")].content
        self.assertTrue(current["protected"])
        self.assertFalse(current["in_course"]["calls"])

    async def test_the_rules_refuse_a_downgrade_even_from_the_module(self) -> None:
        rules = ProtectedRules(self.h.api)
        off = event(COURSE_MANAGEMENT, {"protected": False})
        with module_write():
            self.assertEqual(
                await rules.check_event_allowed(off, state(True)), (False, None)
            )
            self.assertEqual(
                await rules.check_event_allowed(off, state(False)), (True, None)
            )


# --- 6. a retried graduation repairs its effects ---


class TestGraduationRetry(_RouteBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.h.admins.protected.add(ROOM)
        await self.store.start_managed(
            ROOM, STUDENT, actor="system", via="code", joined_at_ms=1, now_ms=2
        )

    async def test_a_retry_reapplies_until_the_student_has_seen_it(self) -> None:
        one = {"room_id": ROOM, "user_ids": [STUDENT]}
        self.assertEqual(
            await self.p.graduate(TEACHER, one), (200, {"graduated": [STUDENT]})
        )
        self.assertEqual(await self.p.graduate(TEACHER, one), (200, {"graduated": []}))
        everyone = {"room_id": ROOM, "all": True}
        self.assertEqual(
            await self.p.graduate(TEACHER, everyone), (200, {"graduated": []})
        )
        self.assertEqual(
            self.graduation.applied,
            [(ROOM, [STUDENT]), (ROOM, [STUDENT]), (ROOM, [STUDENT])],
        )
        # Once seen, the student may have shared their analytics again.
        await self.store.see_graduation(STUDENT, ROOM, 5)
        await self.p.graduate(TEACHER, one)
        self.assertEqual(self.graduation.applied[-1], (ROOM, []))


# --- 7. two graduations in one room keep both power levels ---


class TestPowerLevelsSerialized(_Base):
    async def test_two_graduations_in_one_room_keep_both(self) -> None:
        self.levels: Dict[str, Any] = {
            "users": {TEACHER: 100, STUDENT: 0, OTHER: 0},
            "events_default": 0,
        }
        gate = asyncio.Event()
        reads: List[int] = []

        async def room_state(room_id: str, event_filter: Any) -> Dict:
            types = {t for t, _ in event_filter}
            snapshot = {
                "users": dict(self.levels["users"]),
                "events_default": 0,
            }
            if types == {"m.room.create", "m.room.power_levels"}:
                reads.append(1)
                if len(reads) == 1:
                    await gate.wait()
            return {
                ("m.room.create", ""): SimpleNamespace(
                    type="m.room.create",
                    sender=TEACHER,
                    content={"creator": TEACHER},
                    room_version=SimpleNamespace(msc4289_creator_power_enabled=False),
                ),
                ("m.room.power_levels", ""): SimpleNamespace(
                    type="m.room.power_levels", content=snapshot
                ),
                ("m.room.member", TEACHER): SimpleNamespace(
                    type="m.room.member",
                    state_key=TEACHER,
                    content={"membership": "join"},
                ),
            }

        async def send_event(d: Dict[str, Any]) -> None:
            self.levels = d["content"]

        api = SimpleNamespace(
            _hs=self.h.api._hs,
            get_room_state=room_state,
            create_and_send_event_into_room=send_event,
            is_mine=lambda _: True,
        )
        self.h.main.get_room_max_stream_ordering = lambda: 0
        graduation = Graduation(api, self.h.admins, None)
        graduation._levels = cast(Any, AsyncioLocks())
        first = asyncio.ensure_future(graduation._read_only(ROOM, STUDENT))
        await settle()
        second = asyncio.ensure_future(graduation._read_only(ROOM, OTHER))
        await settle()
        gate.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.levels["users"], {TEACHER: 100, STUDENT: -1, OTHER: -1})


# --- 8. the graduation write requires the record to be managed still ---


class TestGraduationWriteIsConditional(_Base):
    async def test_a_move_between_the_read_and_the_write_wins(self) -> None:
        await self.store.start_managed(
            ROOM, STUDENT, actor="system", via="code", joined_at_ms=1, now_ms=2
        )
        pool = self.h.main.db_pool

        def concurrent_move(sql: str, args: Any) -> None:
            if "SET status = 'graduated'" in sql:
                pool.on_statement = None
                pool.connection.execute(
                    "UPDATE pangea_managed_account SET status = 'moved'"
                    " WHERE user_id = ?",
                    (STUDENT,),
                )

        pool.on_statement = concurrent_move
        self.assertEqual(await self.store.graduate(ROOM, [STUDENT], TEACHER, 10), [])
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual((record["status"], record["graduated_by"]), ("moved", None))
        self.assertNotIn("graduated", [row[2] for row in await self.ledger()])
