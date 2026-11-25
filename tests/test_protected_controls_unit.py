"""The Protected course controls' decisions (pangeachat/.github#799,
managed-accounts "What the course admin controls"): pure functions over a
student's managing courses. The hooks against a real homeserver are in
``test_protected_courses_e2e.py``."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

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


class _Api:
    """``get_room_state`` over a map of course to its management content."""

    def __init__(self, contents: Dict[str, Dict[str, Any]]) -> None:
        self._contents = contents
        self._hs = SimpleNamespace(
            get_datastores=lambda: SimpleNamespace(main=None),
            get_storage_controllers=lambda: None,
        )

    async def get_room_state(self, room_id: str, keys: Any) -> Dict[Any, Any]:
        content = self._contents.get(room_id)
        return {} if content is None else {(COURSE_MANAGEMENT, ""): _State(content)}


class _Store:
    async def managing_courses(self, user_ids: Any) -> Dict[str, List[str]]:
        return {"@s:hs": [A, B, OTHER]}


class _Admins:
    async def is_course_admin(self, room_id: str, user_id: str) -> bool:
        return room_id == B


class ManagingTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_protected_courses_manage(self) -> None:
        # Every seat claim writes a record, in any course: only a Protected
        # course's record makes its controls apply.
        api = _Api({A: {"protected": True}, B: {"protected": True}, OTHER: {}})
        with patch("synapse_pangea_chat.student_invitations.controls.ChatCourses"):
            controls = Controls(api, _Store(), _Admins(), None, ())
        managing = await controls.managing(["@s:hs"])
        self.assertEqual(
            [(c.room_id, c.is_admin) for c in managing["@s:hs"]],
            [(A, False), (B, True)],
        )
