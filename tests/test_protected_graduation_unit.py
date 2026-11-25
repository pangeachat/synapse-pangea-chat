"""Protected courses, graduation (pangeachat/.github#799): the record and
ledger rows a graduation and a support move write, the unseen graduation the
student's status carries, a later join managing again, the routes, and the
power level that makes a graduate's course history read-only.

Over the real store on an in-memory database; the effects on rooms against a
real homeserver are in ``test_protected_courses_e2e.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Tuple, cast

from synapse_pangea_chat.student_invitations.controls import Controls
from synapse_pangea_chat.student_invitations.graduation import Graduation, read_only
from synapse_pangea_chat.student_invitations.protected import ManagedMembership
from synapse_pangea_chat.student_invitations.protected_api import (
    ProtectedCourseHandlers,
)
from tests.test_protected_courses_unit import (
    FakeApi,
    FakeControls,
    _Base,
    member,
    state,
)
from tests.test_seat_invitations_unit import (
    OTHER,
    OTHER_ROOM,
    ROOM,
    STUDENT,
    TEACHER,
    THIRD,
)

OPS = "@ops:x"


class _Managed(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        for user in (STUDENT, OTHER):
            await self.store.start_managed(
                ROOM, user, actor="system", via="code", joined_at_ms=1, now_ms=2
            )
        await self.store.ask(ROOM, [(THIRD, "invite", 3)], TEACHER, 4)


class TestGraduationRecord(_Managed):
    async def test_graduating_one_ends_management_and_writes_a_row(self):
        self.assertEqual(
            await self.store.graduate(ROOM, [STUDENT, THIRD], TEACHER, 10), [STUDENT]
        )
        # Again: nothing left to graduate.
        self.assertEqual(await self.store.graduate(ROOM, [STUDENT], TEACHER, 11), [])
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual(
            (record["status"], record["graduated_by"], record["graduated_at_ms"]),
            ("graduated", TEACHER, 10),
        )
        self.assertEqual(
            await self.store.managing_courses([STUDENT, OTHER]), {OTHER: [ROOM]}
        )
        self.assertEqual(
            (await self.ledger())[-1], (STUDENT, TEACHER, "graduated", "code", None)
        )

    async def test_graduating_the_class_takes_every_managed_student(self):
        self.assertEqual(
            await self.store.graduate(ROOM, None, TEACHER, 10), [OTHER, STUDENT]
        )
        third = await self.store.managed_record(THIRD, ROOM)
        assert third is not None
        self.assertEqual(third["status"], "asked")

    async def test_the_student_sees_a_graduation_once(self):
        await self.store.graduate(ROOM, [STUDENT], TEACHER, 10)
        self.assertEqual(
            await self.store.unseen_graduations(STUDENT),
            [{"room_id": ROOM, "graduated_by": TEACHER, "graduated_at_ms": 10}],
        )
        self.assertTrue(await self.store.see_graduation(STUDENT, ROOM, 12))
        self.assertTrue(await self.store.see_graduation(STUDENT, ROOM, 13))
        self.assertEqual(await self.store.unseen_graduations(STUDENT), [])
        self.assertFalse(await self.store.see_graduation(OTHER, ROOM, 12))

    async def test_a_later_join_of_the_course_manages_again(self):
        await self.store.graduate(ROOM, [STUDENT], TEACHER, 10)
        started: List[str] = []
        callback = ManagedMembership(
            self.h.api, self.store, self.h.claims.unmanaged, started.extend
        )
        self.h.main.membership[(ROOM, STUDENT)] = "join"
        await callback.on_new_event(member(STUDENT, "join", ts=20), state(True))
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual(record["status"], "managed")
        self.assertEqual(started, [STUDENT])
        self.assertEqual(await self.store.unseen_graduations(STUDENT), [])
        self.assertEqual((await self.ledger())[-1][2], "management_started")

    async def test_a_move_manages_by_the_destination_only(self):
        self.assertTrue(
            await self.store.move_managed(STUDENT, ROOM, OTHER_ROOM, OPS, 10)
        )
        self.assertFalse(
            await self.store.move_managed(THIRD, ROOM, OTHER_ROOM, OPS, 11)
        )
        self.assertEqual(
            await self.store.managing_courses([STUDENT]), {STUDENT: [OTHER_ROOM]}
        )
        moved = [r for r in await self.ledger() if r[2] == "moved"]
        self.assertEqual(moved, [(STUDENT, OPS, "moved", "code", f"to {OTHER_ROOM}")])
        # The destination keeps how and when the student joined.
        record = await self.store.managed_record(STUDENT, OTHER_ROOM)
        assert record is not None
        self.assertEqual((record["joined_via"], record["joined_at_ms"]), ("code", 1))
        rows = await self.h.managed_events(OTHER_ROOM)
        self.assertEqual(
            [(e["user_id"], e["action"], e["detail"]) for e in rows],
            [(STUDENT, "moved", f"from {ROOM}")],
        )


class TestReadOnly(_Base):
    def test_the_level_is_below_every_event_the_room_names(self):
        content = {"events_default": 0, "events": {"m.room.name": 50, "p.x": -2}}
        self.assertEqual(
            read_only(content, STUDENT, 0), {**content, "users": {STUDENT: -3}}
        )

    def test_a_student_already_below_is_left_alone(self):
        content = {"events_default": 10, "users": {STUDENT: 5}}
        self.assertIsNone(read_only(content, STUDENT, 5))
        self.assertEqual(read_only({}, STUDENT, 0), {"users": {STUDENT: -1}})

    def test_state_events_count_too(self):
        content = {"events_default": 20, "state_default": 10}
        self.assertEqual(read_only(content, STUDENT, 10)["users"], {STUDENT: 9})
        # Without ``state_default`` the room's default of 50 applies.
        self.assertEqual(
            read_only({"events_default": 20}, STUDENT, 20)["users"], {STUDENT: 19}
        )


class _Courses:
    async def for_room(self, room_id: str, position: int) -> Tuple[str, ...]:
        return (ROOM,)

    def position_now(self) -> int:
        return 0


class _Reconcile:
    async def reconcile(self, user_id: str) -> None:
        return None


class TestGraduationEffects(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sent: List[Dict[str, Any]] = []
        api = SimpleNamespace(
            _hs=self.h.api._hs,
            get_room_state=self.room_state,
            create_and_send_event_into_room=self.send,
            is_mine=lambda _: True,
        )

        async def rooms(_: str) -> set:
            return {ROOM}

        self.h.main.get_rooms_for_user = rooms
        self.h.main.get_room_max_stream_ordering = lambda: 0
        self.graduation = Graduation(api, self.h.admins, _Reconcile())
        self.graduation._courses = cast(Any, _Courses())

    async def room_state(self, room_id: str, event_filter: Any) -> Dict:
        levels = {"users": {TEACHER: 100, STUDENT: 100}, "events_default": 0}
        return {
            ("m.room.create", ""): SimpleNamespace(
                type="m.room.create",
                sender=TEACHER,
                content={"creator": TEACHER},
                room_version=SimpleNamespace(msc4289_creator_power_enabled=False),
            ),
            ("m.room.power_levels", ""): SimpleNamespace(
                type="m.room.power_levels", content=levels
            ),
            ("m.room.member", TEACHER): SimpleNamespace(
                type="m.room.member", state_key=TEACHER, content={"membership": "join"}
            ),
        }

    async def send(self, event: Dict[str, Any]) -> None:
        self.sent.append(event)

    async def test_a_current_course_admin_keeps_their_power_level(self):
        self.h.admins.admins.add((ROOM, STUDENT))
        await self.graduation.apply(ROOM, [STUDENT])
        self.assertEqual(self.sent, [])
        self.h.admins.admins.discard((ROOM, STUDENT))
        await self.graduation.apply(ROOM, [STUDENT])
        self.assertEqual([e["content"]["users"][STUDENT] for e in self.sent], [-1])


class FakeGraduation:
    def __init__(self) -> None:
        self.applied: List[Tuple[str, List[str]]] = []

    def later(self, room_id: str, user_ids: Any) -> None:
        self.applied.append((room_id, list(user_ids)))


class TestGraduationRoutes(_Managed):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.controls = FakeControls()
        self.graduation = FakeGraduation()
        self.h.admins.protected = {ROOM, OTHER_ROOM}
        self.p = ProtectedCourseHandlers(
            FakeApi(self.h),
            self.store,
            self.h.admins,
            self.h.accounts,
            self.h.claims.unmanaged,
            True,
            cast(Controls, self.controls),
            cast(Any, self.graduation),
        )

    async def test_a_course_admin_graduates_one_or_all(self):
        one = {"room_id": ROOM, "user_ids": [STUDENT]}
        self.assertEqual((await self.p.graduate(STUDENT, one))[0], 403)
        self.assertEqual((await self.p.graduate(TEACHER, {"room_id": ROOM}))[0], 400)
        self.assertEqual(
            await self.p.graduate(TEACHER, one), (200, {"graduated": [STUDENT]})
        )
        self.assertEqual(
            await self.p.graduate(TEACHER, {"room_id": ROOM, "all": True}),
            (200, {"graduated": [OTHER]}),
        )
        self.assertEqual(self.graduation.applied, [(ROOM, [STUDENT]), (ROOM, [OTHER])])
        self.h.admins.protected = set()
        status, body = await self.p.graduate(TEACHER, one)
        self.assertEqual((status, body["errcode"]), (409, "ORG.PANGEA.NOT_PROTECTED"))

    async def test_mine_reports_an_unseen_graduation_until_seen(self):
        await self.p.graduate(TEACHER, {"room_id": ROOM, "user_ids": [STUDENT]})
        status, body = await self.p.mine(STUDENT)
        self.assertEqual(status, 200)
        self.assertEqual(
            [g["room_id"] for g in cast(Dict[str, Any], body)["graduations"]], [ROOM]
        )
        seen = await self.p.graduation_seen(STUDENT, {"room_id": ROOM})
        self.assertEqual(seen, (200, {}))
        self.assertEqual(
            await self.p.mine(STUDENT), (200, {"user": STUDENT, "graduations": []})
        )
        self.assertEqual(
            (await self.p.graduation_seen(OTHER, {"room_id": ROOM}))[0], 404
        )

    async def test_only_the_team_moves_a_student(self):
        body = {"user_id": STUDENT, "from_room_id": ROOM, "to_room_id": OTHER_ROOM}
        self.assertEqual((await self.p.move(TEACHER, body))[0], 403)
        self.h.main.server_admins.add(OPS)
        self.assertEqual(await self.p.move(OPS, body), (200, {"moved": True}))
        self.assertEqual(self.controls.reconciled, [[STUDENT]])
        self.assertEqual((await self.p.move(OPS, body))[0], 404)
        same = {**body, "to_room_id": ROOM}
        self.assertEqual((await self.p.move(OPS, same))[0], 400)
        # Both courses must be Protected: an ordinary course's seat record
        # manages no one.
        back = {"user_id": OTHER, "from_room_id": ROOM, "to_room_id": OTHER_ROOM}
        for protected in ({ROOM}, {OTHER_ROOM}):
            self.h.admins.protected = protected
            self.assertEqual((await self.p.move(OPS, back))[0], 409)
        self.assertEqual(await self.store.managing_courses([OTHER]), {OTHER: [ROOM]})
