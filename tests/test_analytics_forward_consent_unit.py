"""The five-fact consent proof, one accept and one reject case per fact.

Parity fixtures for admin-dash-api ``composition._accept_consented_analytics_invites``
(``origin/main`` aa1dd067): every fact it requires is required here, and
dropping any one of them turns an accept below into a bypass.
"""

import unittest
from typing import Any, Dict, List, Mapping, Optional, Tuple
from unittest import mock

from immutabledict import immutabledict

from synapse_pangea_chat.analytics_forward.consent import (
    ACCEPT,
    REFUSED_NO_CLAIM,
    REFUSED_NOT_INVITED,
    REFUSED_OWNER,
    REFUSED_ROOM_TYPE,
    ConsentAcceptor,
    CourseFacts,
    InviteFacts,
    confirmed_below_course_admin,
    consent_verdict,
    course_language,
    is_bot,
    profile_analytics_rooms,
)

STUDENT = "@student:hs"
OTHER = "@other:hs"
TEACHER = "@teacher:hs"
COTEACHER = "@coteacher:hs"
ROOM = "!analytics:hs"


def _invite(**overrides: object) -> InviteFacts:
    values: Dict[str, object] = {
        "room_id": ROOM,
        "invitee": TEACHER,
        "membership": "invite",
        "inviter": STUDENT,
        "creator": STUDENT,
        "room_type": "p.analytics",
    }
    values.update(overrides)
    return InviteFacts(**values)  # type: ignore[arg-type]


def _course(**overrides: object) -> CourseFacts:
    values: Dict[str, object] = {
        "course_id": "!course:hs",
        "invitee_is_admin": True,
        "student_rooms": {STUDENT: [ROOM], OTHER: ["!elsewhere:hs"]},
    }
    values.update(overrides)
    return CourseFacts(**values)  # type: ignore[arg-type]


class TestConsentVerdict(unittest.TestCase):
    def test_all_five_facts_accept(self) -> None:
        self.assertEqual(consent_verdict(_invite(), [_course()]), ACCEPT)

    # Fact 1: the teacher holds an invite.
    def test_fact1_knock_is_not_an_invite(self) -> None:
        self.assertEqual(
            consent_verdict(_invite(membership="knock"), [_course()]),
            REFUSED_NOT_INVITED,
        )

    def test_fact1_no_membership(self) -> None:
        self.assertEqual(
            consent_verdict(_invite(membership=None), [_course()]),
            REFUSED_NOT_INVITED,
        )

    # Fact 2: claimed by exactly one student of a course the teacher runs.
    def test_fact2_teacher_not_admin(self) -> None:
        self.assertEqual(
            consent_verdict(_invite(), [_course(invitee_is_admin=False)]),
            REFUSED_NO_CLAIM,
        )

    def test_fact2_room_not_in_any_profile(self) -> None:
        self.assertEqual(
            consent_verdict(
                _invite(), [_course(student_rooms={STUDENT: ["!other-room:hs"]})]
            ),
            REFUSED_NO_CLAIM,
        )

    def test_fact2_no_shared_course(self) -> None:
        self.assertEqual(consent_verdict(_invite(), []), REFUSED_NO_CLAIM)

    def test_fact2_ambiguous_claim(self) -> None:
        self.assertEqual(
            consent_verdict(
                _invite(), [_course(student_rooms={STUDENT: [ROOM], OTHER: [ROOM]})]
            ),
            REFUSED_NO_CLAIM,
        )

    def test_fact2_second_course_can_prove_it(self) -> None:
        self.assertEqual(
            consent_verdict(
                _invite(),
                [_course(invitee_is_admin=False), _course(course_id="!c2:hs")],
            ),
            ACCEPT,
        )

    def test_fact2_another_course_claimant_does_not_hide_the_proof(self) -> None:
        proving = _course(course_id="!c1:hs")
        other = _course(course_id="!c2:hs", student_rooms={OTHER: [ROOM]})
        for courses in ([proving, other], [other, proving]):
            self.assertEqual(consent_verdict(_invite(), courses), ACCEPT)

    # Fact 3: the invite came from the student.
    def test_fact3_coteacher_invite_is_refused(self) -> None:
        self.assertEqual(
            consent_verdict(_invite(inviter=COTEACHER), [_course()]), REFUSED_OWNER
        )

    # Fact 4: the student created the room.
    def test_fact4_room_created_by_someone_else(self) -> None:
        self.assertEqual(
            consent_verdict(_invite(creator=OTHER), [_course()]), REFUSED_OWNER
        )

    # Fact 5: the room declares itself an analytics room.
    def test_fact5_ordinary_room_is_refused(self) -> None:
        self.assertEqual(
            consent_verdict(_invite(room_type=None), [_course()]), REFUSED_ROOM_TYPE
        )

    def test_fact5_other_room_type_is_refused(self) -> None:
        self.assertEqual(
            consent_verdict(_invite(room_type="m.space"), [_course()]),
            REFUSED_ROOM_TYPE,
        )


class TestProfileRooms(unittest.TestCase):
    PROFILE = {
        "pangea.analytics_profile": {
            "analytics": {
                "es": {"analytics_room_id": "!es:hs"},
                "es-MX": {"analytics_room_id": "!es-legacy:hs"},
                "fr": {"analytics_room_id": "!fr:hs"},
                "de": {"no_room": True},
            }
        }
    }

    def test_known_language_exact_key_wins(self) -> None:
        self.assertEqual(profile_analytics_rooms(self.PROFILE, "es"), ["!es:hs"])
        self.assertEqual(profile_analytics_rooms(self.PROFILE, "es-ES"), ["!es:hs"])

    def test_known_language_without_a_room(self) -> None:
        self.assertEqual(profile_analytics_rooms(self.PROFILE, "de"), [])
        self.assertEqual(profile_analytics_rooms(self.PROFILE, "ja"), [])

    def test_unknown_language_is_every_room(self) -> None:
        self.assertEqual(
            profile_analytics_rooms(self.PROFILE, ""),
            ["!es:hs", "!es-legacy:hs", "!fr:hs"],
        )

    def test_malformed_profiles(self) -> None:
        malformed: Tuple[Any, ...] = (None, {}, {"pangea.analytics_profile": []}, "x")
        for profile in malformed:
            self.assertEqual(profile_analytics_rooms(profile, ""), [])

    def test_event_content_is_an_immutable_mapping(self) -> None:
        # Synapse hands event content over as an immutabledict, not a dict.
        from immutabledict import immutabledict

        self.assertEqual(course_language(immutabledict({"l2": "es"})), "es")
        frozen = immutabledict(
            {
                "pangea.analytics_profile": immutabledict(
                    {
                        "analytics": immutabledict(
                            {"es": immutabledict({"analytics_room_id": "!es:hs"})}
                        )
                    }
                )
            }
        )
        self.assertEqual(profile_analytics_rooms(frozen, "es"), ["!es:hs"])
        powers = immutabledict({"users": immutabledict({TEACHER: 100})})
        self.assertFalse(confirmed_below_course_admin(powers, TEACHER))
        self.assertTrue(confirmed_below_course_admin(powers, STUDENT))

    def test_course_language(self) -> None:
        self.assertEqual(course_language({"l2": "es"}), "es")
        self.assertEqual(course_language({}), "")
        self.assertEqual(course_language({"l2": 3}), "")
        self.assertEqual(course_language(None), "")


class TestRoster(unittest.TestCase):
    def test_student_power(self) -> None:
        powers = {"users": {TEACHER: 100, "@bad:hs": "50", "@flag:hs": True}}
        self.assertTrue(confirmed_below_course_admin(powers, STUDENT))
        self.assertFalse(confirmed_below_course_admin(powers, TEACHER))
        self.assertFalse(confirmed_below_course_admin(powers, "@bad:hs"))
        self.assertFalse(confirmed_below_course_admin(powers, "@flag:hs"))
        self.assertFalse(confirmed_below_course_admin({"users": []}, STUDENT))
        self.assertFalse(confirmed_below_course_admin({"users_default": "0"}, STUDENT))

    def test_bots(self) -> None:
        self.assertTrue(is_bot("@bot:pangea.chat"))
        self.assertTrue(is_bot("@matrix-bot-test:staging.pangea.chat"))
        self.assertTrue(is_bot("@course-bot:hs"))
        self.assertFalse(is_bot(STUDENT))


if __name__ == "__main__":
    unittest.main()


class TestAcceptorJoinsOnlyTheProvenInvite(unittest.IsolatedAsyncioTestCase):
    """The proof awaits course reads; the invite it proved must still be the
    one in place when the join is made."""

    def _acceptor(self, facts_seq: List[InviteFacts]) -> Any:
        acceptor = ConsentAcceptor.__new__(ConsentAcceptor)
        acceptor._api = object()
        reads = list(facts_seq)

        async def invite_facts(room_id: str, invitee: str) -> InviteFacts:
            return reads.pop(0) if len(reads) > 1 else reads[0]

        async def course_facts(invitee: str, inviter: str) -> List[CourseFacts]:
            return [_course()]

        async def join_position(room_id: str, invitee: str) -> int:
            return 9

        acceptor.invite_facts = invite_facts  # type: ignore[method-assign]
        acceptor.course_facts = course_facts  # type: ignore[method-assign]
        acceptor._join_position = join_position  # type: ignore[method-assign]
        return acceptor

    async def test_unchanged_invite_is_joined(self) -> None:
        acceptor = self._acceptor([_invite()])
        acceptor._join = mock.AsyncMock(return_value=9)
        verdict = await acceptor.accept_if_consented(ROOM, TEACHER)
        self.assertEqual(verdict, (ACCEPT, 9))
        acceptor._join.assert_awaited_once()

    async def test_fence_is_the_join_just_made(self) -> None:
        """The join's own position, even if the teacher has left again by the
        time anything reads current membership."""
        acceptor = self._acceptor([_invite()])

        async def left_already(room_id: str, invitee: str) -> int:
            return 0

        acceptor._join_position = left_already
        acceptor._join = mock.AsyncMock(return_value=10)
        self.assertEqual(
            await acceptor.accept_if_consented(ROOM, TEACHER), (ACCEPT, 10)
        )
        acceptor._join.assert_awaited_once_with(ROOM, TEACHER)

    async def test_replaced_invite_is_not_joined(self) -> None:
        acceptor = self._acceptor([_invite(), _invite(inviter=COTEACHER)])
        acceptor._join = mock.AsyncMock(return_value=9)
        verdict = await acceptor.accept_if_consented(ROOM, TEACHER)
        self.assertEqual(verdict, (REFUSED_NOT_INVITED, 0))
        acceptor._join.assert_not_awaited()


class _Event:
    def __init__(self, sender: str, content: Mapping[str, Any]) -> None:
        self.sender = sender
        self.content = content


class TestCourseLanguageThroughTheFactReads(unittest.IsolatedAsyncioTestCase):
    """The real ``invite_facts`` + ``course_facts`` over a fake homeserver: a
    student with rooms in two languages consents only for the course's."""

    ES_ROOM = "!es:hs"
    FR_ROOM = "!fr:hs"

    def _acceptor(self, l2: Optional[str]) -> Any:
        test = self
        plan: Dict[str, Any] = {"uuid": "plan"}
        if l2 is not None:
            plan["l2"] = l2

        class Store:
            async def get_local_current_membership_for_user_in_room(
                self, user_id: str, room_id: str
            ) -> Tuple[str, str]:
                return "invite", f"$invite-{room_id}"

            async def get_event(self, event_id: str, allow_none: bool = False) -> Any:
                return _Event(STUDENT, {"membership": "invite"})

            async def get_rooms_for_user(self, user_id: str) -> frozenset:
                return frozenset({"!course:hs"})

            async def get_users_in_room(self, room_id: str) -> List[str]:
                return [TEACHER, STUDENT]

            async def get_profile_data_for_users(self, users: Any) -> Dict[str, Any]:
                return {
                    STUDENT: {
                        "pangea.analytics_profile": {
                            "analytics": {
                                "es": {"analytics_room_id": test.ES_ROOM},
                                "fr": {"analytics_room_id": test.FR_ROOM},
                            }
                        }
                    }
                }

        class State:
            async def get_current_state_event(
                self, room_id: str, etype: str, key: str
            ) -> Any:
                return _Event(STUDENT, {"type": "p.analytics"})

            async def get_current_state(self, room_id: str, state_filter: Any) -> Any:
                return {
                    ("pangea.course_plan", ""): _Event(TEACHER, immutabledict(plan)),
                    ("m.room.power_levels", ""): _Event(
                        TEACHER, {"users": {TEACHER: 100}}
                    ),
                }

        acceptor = ConsentAcceptor.__new__(ConsentAcceptor)
        acceptor._api = object()
        acceptor._store = Store()
        acceptor._state = State()
        acceptor._course_filter = mock.sentinel.state_filter

        async def join_position(room_id: str, invitee: str) -> int:
            return 9

        acceptor._join_position = join_position  # type: ignore[method-assign]
        return acceptor

    async def _verdict(self, l2: Optional[str], room_id: str) -> Tuple[str, int]:
        acceptor = self._acceptor(l2)
        acceptor._join = mock.AsyncMock(return_value=9)
        return await acceptor.accept_if_consented(room_id, TEACHER)

    async def test_course_language_room_is_accepted(self) -> None:
        self.assertEqual(await self._verdict("es", self.ES_ROOM), (ACCEPT, 9))

    async def test_other_language_room_is_refused(self) -> None:
        self.assertEqual(await self._verdict("es", self.FR_ROOM), (REFUSED_NO_CLAIM, 0))

    async def test_regioned_course_language_matches(self) -> None:
        self.assertEqual(await self._verdict("es-MX", self.ES_ROOM), (ACCEPT, 9))
        self.assertEqual(
            await self._verdict("es-MX", self.FR_ROOM), (REFUSED_NO_CLAIM, 0)
        )

    async def test_no_course_language_uses_every_room(self) -> None:
        self.assertEqual(await self._verdict(None, self.FR_ROOM), (ACCEPT, 9))
