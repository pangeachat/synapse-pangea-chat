"""The Protected course controls' decisions (pangeachat/.github#799,
managed-accounts "What the course admin controls"): pure functions over a
student's managing courses. The hooks against a real homeserver are in
``test_protected_courses_e2e.py``."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

from synapse.api.errors import Codes

from synapse_pangea_chat.student_invitations.controls import (
    CALLS,
    DMS,
    OTHER_COURSES,
    Controls,
    Managing,
    call_in_room,
    effective,
    in_room,
    is_call,
    outside,
    with_person,
)
from synapse_pangea_chat.student_invitations.protected import (
    COURSE_MANAGEMENT,
    management,
)

A, B, OTHER = "!a:hs", "!b:hs", "!other:hs"
CHAT = "!chat:hs"


def course(room_id: str, admin: bool = False, **groups: Dict[str, bool]) -> Managing:
    content: Dict[str, Any] = {"protected": True, **groups}
    return Managing(room_id, management(content), admin)


class InRoomTest(unittest.TestCase):
    def test_another_course_needs_the_outside_control(self) -> None:
        self.assertEqual(in_room([course(A)], OTHER, (OTHER,)), "outside.other_courses")
        self.assertEqual(in_room([course(A)], CHAT, (OTHER,)), "outside.other_courses")
        on = course(A, outside={OTHER_COURSES: True})
        self.assertIsNone(in_room([on], CHAT, (OTHER,)))

    def test_the_course_space_itself_is_always_allowed(self) -> None:
        closed = course(A, in_course={OTHER_COURSES: False})
        self.assertIsNone(in_room([closed], A, (A,)))

    def test_the_course_chats_need_the_in_course_control(self) -> None:
        self.assertIsNone(in_room([course(A)], CHAT, (A,)))
        closed = course(A, in_course={OTHER_COURSES: False})
        self.assertEqual(in_room([closed], CHAT, (A,)), "in_course.other_courses")

    def test_a_room_no_course_owns_is_not_governed(self) -> None:
        self.assertIsNone(in_room([course(A)], CHAT, ()))

    def test_the_stricter_course_wins(self) -> None:
        on = course(A, outside={OTHER_COURSES: True})
        self.assertEqual(
            in_room([on, course(B)], CHAT, (OTHER,)), "outside.other_courses"
        )

    def test_another_managing_course_is_another_course_to_each(self) -> None:
        # Managed by A and B: B's rooms are outside A, so A must allow them.
        self.assertEqual(
            in_room([course(A), course(B)], CHAT, (B,)), "outside.other_courses"
        )
        self.assertEqual(
            in_room([course(A), course(B)], B, (B,)), "outside.other_courses"
        )
        a_on = course(A, outside={OTHER_COURSES: True})
        self.assertIsNone(in_room([a_on, course(B)], B, (B,)))
        self.assertIsNone(in_room([a_on, course(B)], CHAT, (B,)))

    def test_controls_skip_a_course_the_student_is_admin_of(self) -> None:
        self.assertIsNone(in_room([course(A, admin=True)], CHAT, (OTHER,)))
        closed = course(A, admin=True, in_course={OTHER_COURSES: False})
        self.assertIsNone(in_room([closed], CHAT, (A,)))


class WithPersonTest(unittest.TestCase):
    def test_someone_outside_needs_the_outside_control(self) -> None:
        self.assertEqual(with_person([course(A)], DMS, set()), "outside.dms")
        self.assertEqual(with_person([course(A)], CALLS, set()), "outside.calls")
        on = course(A, outside={DMS: True})
        self.assertIsNone(with_person([on], DMS, set()))

    def test_a_classmate_needs_the_in_course_control(self) -> None:
        self.assertIsNone(with_person([course(A)], DMS, {A}))
        closed = course(A, in_course={DMS: False})
        self.assertEqual(with_person([closed], DMS, {A}), "in_course.dms")

    def test_the_stricter_course_wins(self) -> None:
        # A classmate in A, an outsider to B.
        self.assertEqual(with_person([course(A), course(B)], DMS, {A}), "outside.dms")

    def test_an_admin_course_is_skipped(self) -> None:
        self.assertIsNone(with_person([course(A, admin=True)], DMS, set()))


class CallsTest(unittest.TestCase):
    def test_calls_in_the_course_rooms_and_elsewhere(self) -> None:
        self.assertIsNone(call_in_room([course(A)], (A,)))
        closed = course(A, in_course={CALLS: False})
        self.assertEqual(call_in_room([closed], (A,)), "in_course.calls")
        self.assertEqual(call_in_room([course(A)], (OTHER,)), "outside.calls")
        self.assertEqual(call_in_room([course(A)], ()), "outside.calls")

    def test_call_event_types(self) -> None:
        for kind in (
            "m.call.invite",
            "org.matrix.call.sdp_stream_metadata_changed",
            "com.famedly.call.member",
            "org.matrix.msc3401.call.member",
            "org.matrix.msc3401.call",
        ):
            self.assertTrue(is_call(kind), kind)
        for kind in ("m.room.message", "m.reaction", "m.callout", "pangea.call"):
            self.assertFalse(is_call(kind), kind)


class OutsideAndEffectiveTest(unittest.TestCase):
    def test_browsing_and_creating_a_course(self) -> None:
        self.assertEqual(outside([course(A)], OTHER_COURSES), "outside.other_courses")
        on = course(A, outside={OTHER_COURSES: True})
        self.assertIsNone(outside([on], OTHER_COURSES))
        self.assertIsNone(outside([], OTHER_COURSES))

    def test_effective_controls_are_the_stricter_of_each(self) -> None:
        a = course(A, outside={DMS: True, CALLS: True})
        b = course(B, outside={DMS: True}, in_course={CALLS: False})
        self.assertEqual(
            effective([a, b, course(OTHER, admin=True)]),
            {
                "outside": {OTHER_COURSES: False, DMS: True, CALLS: False},
                "in_course": {OTHER_COURSES: True, DMS: True, CALLS: False},
            },
        )

    def test_nothing_is_restricted_without_a_managing_course(self) -> None:
        self.assertTrue(all(all(g.values()) for g in effective([]).values()))


if __name__ == "__main__":
    unittest.main()


class _State:
    def __init__(self, content: Dict[str, Any]) -> None:
        self.content = content


class _Main:
    def __init__(self) -> None:
        self.joined: List[str] = []

    async def get_users_in_room(self, room_id: str) -> List[str]:
        return self.joined


class _Api:
    """``get_room_state`` over a map of course to its management content."""

    def __init__(self, contents: Dict[str, Dict[str, Any]]) -> None:
        self._contents = contents
        self.main = _Main()
        self.state_reads = 0
        self._hs = SimpleNamespace(
            get_datastores=lambda: SimpleNamespace(main=self.main),
            get_storage_controllers=lambda: None,
        )

    async def get_room_state(self, room_id: str, keys: Any) -> Dict[Any, Any]:
        self.state_reads += 1
        content = self._contents.get(room_id)
        return {} if content is None else {(COURSE_MANAGEMENT, ""): _State(content)}


class _Store:
    def __init__(self) -> None:
        self.calls: List[str] = []
        self.broken = False

    async def managed_rows(self) -> List[Tuple[str, str]]:
        self.calls.append("managed_rows")
        if self.broken:
            raise RuntimeError("down")
        return [("@s:hs", A), ("@s:hs", B), ("@s:hs", OTHER), ("@seat:hs", OTHER)]

    async def managing_courses(self, user_ids: Any) -> Dict[str, List[str]]:
        self.calls.append("managing_courses")
        if self.broken:
            raise RuntimeError("down")
        return {"@s:hs": [A, B, OTHER]} if "@s:hs" in user_ids else {}


class _Admins:
    async def is_course_admin(self, room_id: str, user_id: str) -> bool:
        return room_id == B


class ManagingTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_protected_courses_manage(self) -> None:
        # Every seat claim writes a record, in any course: only a Protected
        # course's record makes its controls apply.
        api = _Api({A: {"protected": True}, B: {"protected": True}, OTHER: {}})
        with patch(CHAT_COURSES):
            controls = Controls(api, _Store(), _Admins(), None, ())
        managing = await controls.managing(["@s:hs"])
        self.assertEqual(
            [(c.room_id, c.is_admin) for c in managing["@s:hs"]],
            [(A, False), (B, True)],
        )


CHAT_COURSES = "synapse_pangea_chat.student_invitations.controls.ChatCourses"
REPORT = "synapse_pangea_chat.student_invitations.controls.report_failure"
U, V, W = "@u:hs", "@v:hs", "@w:hs"


class UnmanagedCostTest(unittest.IsolatedAsyncioTestCase):
    """An unmanaged user costs no read of the module's and is never failed by
    it; a managed one is refused when their controls cannot be read."""

    async def asyncSetUp(self) -> None:
        self.api = _Api({A: {"protected": True}, B: {"protected": True}})
        self.store = _Store()
        with patch(CHAT_COURSES):
            self.controls = Controls(self.api, self.store, _Admins(), None, ())

    async def actions(self, user: str, other: str) -> List[Any]:
        send = SimpleNamespace(
            sender=user, room_id="!r:hs", type="m.room.message", redacts=None
        )
        return [
            await self.controls.check_event_for_spam(send),
            await self.controls.user_may_join_room(user, "!r:hs", False),
            await self.controls.user_may_invite(user, other, "!r:hs"),
            await self.controls.user_may_create_room(user, {"invite": [other]}),
            await self.controls.user_may_send_3pid_invite(user, "email", "x", "!r:hs"),
        ]

    async def test_unmanaged_actions_read_nothing_once_loaded(self) -> None:
        await self.controls.refresh()
        self.assertEqual(await self.controls.known(), frozenset({"@s:hs"}))
        self.store.calls.clear()
        self.api.state_reads = 0
        # A seat claimant of a non-Protected course is not managed; a group
        # room with a managed member is judged on its sender alone.
        self.api.main.joined = [U, V, "@s:hs"]
        self.assertEqual(await self.actions("@seat:hs", U), ["NOT_SPAM"] * 5)
        self.assertEqual((self.store.calls, self.api.state_reads), ([], 0))

    async def test_unmanaged_actions_are_allowed_when_the_store_fails(self) -> None:
        self.store.broken = True
        with patch(REPORT) as report:
            self.assertEqual(await self.actions(U, V), ["NOT_SPAM"] * 5)
        self.assertTrue(report.called)
        self.store.calls.clear()
        self.controls._managed = None
        with patch(REPORT):
            status = await self.controls.status(U)
        self.assertEqual(status["courses"], [])
        # The failed first read is not retried by each request.
        self.assertEqual(self.store.calls, ["managed_rows"])

    async def test_a_managed_party_is_refused_when_the_store_fails(self) -> None:
        await self.controls.refresh()
        self.store.broken = True
        with patch(REPORT):
            answers = await self.actions("@s:hs", U)
        self.assertEqual(
            answers, [(Codes.FORBIDDEN, {"org.pangea.control": "unavailable"})] * 5
        )
        # Both ways: someone unmanaged inviting the managed student.
        with patch(REPORT):
            self.assertEqual(
                await self.controls.user_may_invite(U, "@s:hs", "!r:hs"),
                (Codes.FORBIDDEN, {"org.pangea.control": "unavailable"}),
            )

    async def test_the_modules_own_writes_add_to_the_set(self) -> None:
        await self.controls.refresh()
        with patch.object(self.api, "run_as_background_process", create=True):
            self.controls.reconcile_later([W])
        self.assertIn(W, await self.controls.known())

    async def test_protected_courses_for_the_safety_page(self) -> None:
        # Only the Protected courses managing the sender, the one they are
        # admin of included, read from the records even before this
        # process's set knows the student.
        self.assertEqual(await self.controls.protected_courses("@s:hs"), (A, B))
        self.assertEqual(self.store.calls, ["managing_courses"])
        self.assertEqual(await self.controls.protected_courses("@seat:hs"), ())
        self.store.broken = True
        with self.assertRaises(RuntimeError):
            await self.controls.protected_courses("@s:hs")
