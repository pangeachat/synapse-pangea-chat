"""Discussion rooms without a homeserver: which rooms are a discussion's, who
counts toward a room's students, how seats are written and ended, the room's
power levels, the CMS lookup and the direct-join check.

Design: .github/instructions/discussion-room.instructions.md.
"""

import json
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, Mock, patch

from synapse.api.errors import Codes
from synapse.module_api import NOT_SPAM

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.discussion_room import discussion_room, join_check
from synapse_pangea_chat.discussion_room.discussion_room import (
    ERRCODE_REMOVED,
    ERRCODE_UNKNOWN_VERSION,
    ChoreoUnavailable,
    DiscussionRoom,
    _Refused,
    _Version,
    _version_id,
)
from synapse_pangea_chat.discussion_room.join_check import DiscussionJoinCheck
from synapse_pangea_chat.discussion_room.rooms import (
    ACTIVITY_PLAN_EVENT,
    ACTIVITY_ROLES_EVENT,
    ACTIVITY_SUMMARY_EVENT,
    DISCUSSION_MARKER,
    DiscussionMarker,
    continued_seat,
    creation_content,
    discussion_marker,
    discussion_power_levels,
    ended_seat,
    find_seat,
    is_discussion_plan,
    is_removal,
    mirrored_admin_levels,
    pick_room,
    room_view,
    seat_holders,
    seat_key,
    utc_timestamp,
    with_seat,
    with_seat_changed,
    without_seat,
)
from synapse_pangea_chat.email_invite.course_missions import course_copy_lo_ids

BOT = "@discussion-bot:example.com"
COURSE = "!course:example.com"
OTHER_COURSE = "!other-course:example.com"
ROOM = "!room:example.com"
ACTIVITY = "act-1"
TEACHER = "@teacher:example.com"
CAPACITY = 12
COURSE_MISSIONS_LOGGER = (
    "synapse.module.synapse_pangea_chat.email_invite.course_missions"
)


def _student(n: int) -> str:
    return f"@student{n}:example.com"


def _member(
    user_id: str,
    membership: str,
    sender: Optional[str] = None,
    prev: Optional[str] = None,
) -> Any:
    """A member event; ``prev`` is the membership it replaced, as the store
    gives it with ``prev_content``."""
    return SimpleNamespace(
        content={"membership": membership},
        sender=sender or user_id,
        state_key=user_id,
        unsigned={"prev_content": {"membership": prev}} if prev else {},
    )


def _roles(*user_ids: str, **seat_fields: Any) -> Any:
    content: Dict[str, Any] = {}
    for user_id in user_ids:
        content = with_seat(content, user_id)
    if seat_fields:
        roles = content["roles"]
        for key in roles:
            roles[key] = {**roles[key], **seat_fields}
    return SimpleNamespace(content=content, sender=BOT)


def _state(
    members: Dict[str, Any], roles: Optional[Any] = None
) -> Dict[Tuple[str, str], Any]:
    state: Dict[Tuple[str, str], Any] = {
        ("m.room.member", user_id): event for user_id, event in members.items()
    }
    if roles is not None:
        state[(ACTIVITY_ROLES_EVENT, "")] = roles
    return state


def _cms_role(role_id: str = "participant") -> Dict[str, Any]:
    """A role as an ``activities-v2`` row stores it: Payload gives every
    array item its own row ``id``."""
    return {
        "id": "6650f1a2b3c4d5e6f7a8b9c0",
        "role_id": role_id,
        "name": "Participant",
        "goal_ids": ["q1", "q2"],
    }


def _discussion_plan(**overrides: Any) -> Dict[str, Any]:
    plan = {
        "activity_id": ACTIVITY,
        "mode": "Discussion",
        "title": "Weekend plans",
        "roles": [_cms_role()],
    }
    plan.update(overrides)
    return plan


# --- Which rooms and plans are discussions ------------------------------------


class TestIsDiscussionPlan(unittest.TestCase):
    def test_a_cms_row_whose_role_item_carries_payloads_own_id(self):
        # Regression (local testing, 2026-11-03): the Payload row `id` was
        # read before `role_id`, so every real discussion was refused.
        self.assertTrue(is_discussion_plan(_discussion_plan()))

    def test_the_mode_in_any_case(self):
        for mode in ("discussion", "DISCUSSION", " Discussion "):
            self.assertTrue(is_discussion_plan(_discussion_plan(mode=mode)), mode)

    def test_a_role_keyed_by_its_id_in_other_shapes(self):
        self.assertTrue(
            is_discussion_plan(_discussion_plan(roles=[{"id": "participant"}]))
        )
        self.assertTrue(
            is_discussion_plan(
                _discussion_plan(roles={"participant": {"name": "Participant"}})
            )
        )

    def test_an_old_free_text_discussion_shaped_like_a_roleplay_is_not(self):
        roles = [_cms_role("moderator"), _cms_role("debater")]
        self.assertFalse(is_discussion_plan(_discussion_plan(roles=roles)))

    def test_one_role_that_is_not_participant_is_not(self):
        plan = _discussion_plan(roles=[_cms_role("moderator")])
        self.assertFalse(is_discussion_plan(plan))

    def test_other_modes_and_missing_roles_are_not(self):
        self.assertFalse(is_discussion_plan(_discussion_plan(mode="Roleplay")))
        self.assertFalse(is_discussion_plan(_discussion_plan(mode=None)))
        self.assertFalse(is_discussion_plan(_discussion_plan(roles=None)))
        self.assertFalse(is_discussion_plan(_discussion_plan(roles=[])))


class TestDiscussionMarker(unittest.TestCase):
    def test_a_room_the_bot_created_with_the_marker(self):
        content = creation_content(COURSE, ACTIVITY)
        self.assertEqual(
            discussion_marker(BOT, content, BOT), DiscussionMarker(COURSE, ACTIVITY)
        )

    def test_a_room_a_student_made_to_look_like_one_is_not(self):
        content = creation_content(COURSE, ACTIVITY)
        self.assertIsNone(discussion_marker(_student(1), content, BOT))

    def test_a_marker_naming_another_activity_than_the_room_type_is_not(self):
        content = creation_content(COURSE, ACTIVITY)
        content["type"] = "p.activity.session:other"
        self.assertIsNone(discussion_marker(BOT, content, BOT))

    def test_a_bot_room_without_the_marker_is_not(self):
        content = {"type": f"p.activity.session:{ACTIVITY}"}
        self.assertIsNone(discussion_marker(BOT, content, BOT))
        content[DISCUSSION_MARKER] = {"course_id": COURSE}
        self.assertIsNone(discussion_marker(BOT, content, BOT))

    def test_nothing_is_a_discussion_room_without_a_bot(self):
        content = creation_content(COURSE, ACTIVITY)
        self.assertIsNone(discussion_marker(BOT, content, None))
        self.assertIsNone(discussion_marker(BOT, None, BOT))


# --- Seats ------------------------------------------------------------------


class TestSeats(unittest.TestCase):
    def test_a_seat_is_keyed_by_student(self):
        content = with_seat(None, _student(1))
        self.assertEqual(
            content,
            {
                "roles": {
                    f"participant:{_student(1)}": {
                        "id": "participant",
                        "user_id": _student(1),
                        "role": "Participant",
                    }
                }
            },
        )
        self.assertEqual(seat_key(_student(1)), f"participant:{_student(1)}")

    def test_adding_a_seat_keeps_everyone_elses_and_other_keys(self):
        content = with_seat({"other": 1, "roles": {}}, _student(1))
        content = with_seat(content, _student(2))
        self.assertEqual(content["other"], 1)
        self.assertEqual(seat_holders(content), {_student(1), _student(2)})

    def test_an_existing_seat_is_kept_as_it_is(self):
        ended = ended_seat(
            with_seat(None, _student(1))["roles"][seat_key(_student(1))], "T1"
        )
        content = {"roles": {"legacy-key": ended}}
        self.assertEqual(with_seat(content, _student(1)), content)

    def test_find_and_remove_a_seat(self):
        content = with_seat(with_seat(None, _student(1)), _student(2))
        found = find_seat(content, _student(2))
        assert found is not None
        self.assertEqual(found[0], seat_key(_student(2)))
        self.assertIsNone(find_seat(content, _student(3)))
        self.assertEqual(
            seat_holders(without_seat(content, _student(2))), {_student(1)}
        )

    def test_malformed_seats_are_skipped(self):
        content = {"roles": {"a": "nope", "b": {"user_id": 7}, "c": {"id": "x"}}}
        self.assertEqual(seat_holders(content), frozenset())
        self.assertEqual(seat_holders({"roles": []}), frozenset())

    def test_ending_keeps_the_first_end_and_continuing_clears_it(self):
        seat = with_seat(None, _student(1))["roles"][seat_key(_student(1))]
        ended = ended_seat(seat, "T1")
        self.assertEqual((ended["finished_at"], ended["archived_at"]), ("T1", "T1"))
        self.assertEqual(ended_seat(ended, "T2"), ended)
        reopened = continued_seat(ended)
        self.assertIsNone(reopened["finished_at"])
        self.assertIsNone(reopened["archived_at"])
        self.assertEqual(reopened["user_id"], _student(1))

    def test_changing_one_seat_leaves_the_others(self):
        content = with_seat(with_seat(None, _student(1)), _student(2))
        key = seat_key(_student(1))
        changed = with_seat_changed(
            content, key, ended_seat(content["roles"][key], "T")
        )
        self.assertEqual(changed["roles"][key]["finished_at"], "T")
        self.assertEqual(
            changed["roles"][seat_key(_student(2))],
            content["roles"][seat_key(_student(2))],
        )

    def test_timestamps_match_the_clients_format(self):
        self.assertEqual(utc_timestamp(1762264800.1234), "2025-11-04T14:00:00.123Z")


# --- Who counts toward a room's students --------------------------------------


class TestRoomCount(unittest.TestCase):
    def _view(self, members: Dict[str, Any], roles: Any, joining=frozenset()) -> Any:
        return room_view(ROOM, _state(members, roles), BOT, 0, joining)

    def test_joined_seat_holders_count(self):
        students = [_student(n) for n in range(3)]
        members = {u: _member(u, "join") for u in students + [BOT]}
        self.assertEqual(self._view(members, _roles(*students)).students, 3)

    def test_ended_students_still_count(self):
        members = {u: _member(u, "join") for u in (_student(1), BOT)}
        roles = _roles(_student(1), finished_at="T", archived_at="T")
        self.assertEqual(self._view(members, roles).students, 1)

    def test_a_seat_counts_while_its_join_is_under_way(self):
        # Written ahead of a join still on its way.
        members = {BOT: _member(BOT, "join")}
        view = self._view(members, _roles(_student(1)), {_student(1)})
        self.assertEqual(view.students, 1)
        self.assertEqual(view.stale, frozenset())
        self.assertTrue(view.is_own(_student(1)))

    def test_a_seat_whose_join_never_landed_is_stale(self):
        # Its join failed without the seat being taken back, or a restart cut
        # it off: it neither counts nor makes the room the holder's own.
        members = {BOT: _member(BOT, "join"), _student(2): _member(_student(2), "join")}
        view = self._view(members, _roles(_student(1), _student(2)))
        self.assertEqual(view.students, 1)
        self.assertEqual(view.stale, {_student(1)})
        self.assertFalse(view.is_own(_student(1)))
        self.assertFalse(view.refuses(_student(1)))

    def test_admins_reading_along_and_the_bot_do_not_count(self):
        members = {u: _member(u, "join") for u in (TEACHER, BOT, _student(1))}
        # Even a seat written for the bot is not a student.
        view = self._view(members, _roles(_student(1), BOT))
        self.assertEqual(view.students, 1)
        self.assertTrue(view.bot_joined)

    def test_an_admin_who_took_a_seat_counts(self):
        members = {u: _member(u, "join") for u in (TEACHER, BOT)}
        self.assertEqual(self._view(members, _roles(TEACHER)).students, 1)

    def test_seats_of_students_who_left_were_removed_or_banned_do_not_count(self):
        left, kicked, banned = _student(1), _student(2), _student(3)
        members = {
            BOT: _member(BOT, "join"),
            left: _member(left, "leave"),
            kicked: _member(kicked, "leave", sender=TEACHER),
            banned: _member(banned, "ban", sender=TEACHER),
        }
        self.assertEqual(self._view(members, _roles(left, kicked, banned)).students, 0)

    def test_own_room_and_refusals(self):
        joined, pending, left, kicked, banned, stranger = (
            _student(n) for n in range(6)
        )
        members = {
            BOT: _member(BOT, "join"),
            joined: _member(joined, "join"),
            left: _member(left, "leave"),
            kicked: _member(kicked, "leave", sender=TEACHER),
            banned: _member(banned, "ban", sender=TEACHER),
        }
        view = self._view(
            members, _roles(joined, pending, left, kicked, banned), {pending}
        )
        for user_id in (joined, pending, left):
            self.assertTrue(view.is_own(user_id), user_id)
            self.assertFalse(view.refuses(user_id), user_id)
        for user_id in (kicked, banned):
            self.assertFalse(view.is_own(user_id), user_id)
            self.assertTrue(view.refuses(user_id), user_id)
        self.assertFalse(view.is_own(stranger))
        self.assertFalse(view.refuses(stranger))

    def test_an_unban_is_not_a_removal(self):
        unbanned, kicked = _student(1), _student(2)
        members = {
            BOT: _member(BOT, "join"),
            unbanned: _member(unbanned, "leave", sender=TEACHER, prev="ban"),
            kicked: _member(kicked, "leave", sender=TEACHER, prev="join"),
        }
        view = self._view(members, _roles(unbanned, kicked))
        self.assertFalse(view.refuses(unbanned))
        self.assertTrue(view.is_own(unbanned))
        self.assertTrue(view.refuses(kicked))
        self.assertFalse(is_removal(members[unbanned]))
        self.assertTrue(is_removal(members[kicked]))
        self.assertTrue(is_removal(_member(kicked, "ban", sender=TEACHER)))
        self.assertFalse(is_removal(_member(kicked, "leave")))

    def test_a_withdrawn_invite_or_rejected_knock_is_not_a_removal(self):
        withdrawn, rejected = _student(1), _student(2)
        members = {
            BOT: _member(BOT, "join"),
            withdrawn: _member(withdrawn, "leave", sender=TEACHER, prev="invite"),
            rejected: _member(rejected, "leave", sender=TEACHER, prev="knock"),
        }
        view = self._view(members, None)
        for user_id in (withdrawn, rejected):
            self.assertFalse(view.refuses(user_id), user_id)
            self.assertFalse(is_removal(members[user_id]), user_id)

    def test_a_room_whose_bot_is_gone_reads_as_such(self):
        members = {BOT: _member(BOT, "leave", sender=TEACHER)}
        self.assertFalse(self._view(members, None).bot_joined)


class TestPickRoom(unittest.TestCase):
    def test_eleven_admits_one_more(self):
        self.assertEqual(pick_room([("!a", 11)], CAPACITY), "!a")

    def test_twelve_opens_a_new_room(self):
        self.assertIsNone(pick_room([("!a", 12)], CAPACITY))
        self.assertIsNone(pick_room([], CAPACITY))

    def test_the_fullest_room_with_space_ties_to_the_first_listed(self):
        rooms = [("!a", 3), ("!b", 12), ("!c", 7), ("!d", 7)]
        self.assertEqual(pick_room(rooms, CAPACITY), "!c")


# --- Power levels --------------------------------------------------------------


class TestPowerLevels(unittest.TestCase):
    def test_the_rooms_shape(self):
        levels = discussion_power_levels(BOT, frozenset({TEACHER}))
        self.assertEqual(levels["users"], {BOT: 100, TEACHER: 50})
        self.assertEqual(levels["users_default"], 0)
        # Admins moderate students but can't remove the bot or change levels.
        self.assertEqual(
            (levels["kick"], levels["ban"], levels["redact"]), (50, 50, 50)
        )
        # Only the bot invites: Join is the only way in.
        self.assertEqual(levels["invite"], 100)
        self.assertEqual(levels["events"]["m.room.power_levels"], 100)
        # Only the server writes the plan and the seats.
        self.assertEqual(levels["events"][ACTIVITY_PLAN_EVENT], 100)
        self.assertEqual(levels["events"][ACTIVITY_ROLES_EVENT], 100)
        self.assertEqual(levels["events"][ACTIVITY_SUMMARY_EVENT], 0)
        self.assertEqual(levels["events_default"], 0)
        # Every room setting not listed (name, join rules, encryption, history
        # visibility, tombstone) is the bot's alone.
        self.assertEqual(levels["state_default"], 100)
        for event_type, level in levels["events"].items():
            if level < 100:
                self.assertIn(
                    event_type, (ACTIVITY_SUMMARY_EVENT, "m.room.pinned_events")
                )

    def test_the_bot_stays_at_100_even_if_listed_as_an_admin(self):
        levels = discussion_power_levels(BOT, frozenset({BOT, TEACHER}))
        self.assertEqual(levels["users"][BOT], 100)


class TestMirroredAdminLevels(unittest.TestCase):
    def test_a_new_admin_is_raised_and_a_former_one_removed(self):
        users = {BOT: 100, TEACHER: 50, _student(1): 0}
        new_admin = "@coteacher:example.com"
        self.assertEqual(
            mirrored_admin_levels(users, BOT, frozenset({new_admin})),
            {BOT: 100, new_admin: 50, _student(1): 0},
        )

    def test_nothing_changes_when_in_step(self):
        users = {BOT: 100, TEACHER: 50}
        self.assertIsNone(mirrored_admin_levels(users, BOT, frozenset({TEACHER})))

    def test_the_bot_and_other_levels_are_left_alone(self):
        users = {BOT: 100, "@helper:example.com": 75}
        self.assertIsNone(mirrored_admin_levels(users, BOT, frozenset({BOT})))
        self.assertIsNone(
            mirrored_admin_levels(users, BOT, frozenset({"@helper:example.com"}))
        )


# --- The start page's version ---------------------------------------------------


class TestVersionId(unittest.TestCase):
    def test_only_a_content_signature_is_pinned(self):
        signature = "0123456789abcdef0123456789abcdef"
        self.assertEqual(_version_id(signature, ACTIVITY), signature)
        with self.assertLogs(discussion_room.logger, "WARNING"):
            self.assertIsNone(_version_id(None, ACTIVITY))
        with self.assertLogs(discussion_room.logger, "WARNING"):
            self.assertIsNone(_version_id("v2", ACTIVITY))


# --- The CMS lookup ---------------------------------------------------------------


def _row(plan: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    return {"id": "row-1", "req": {}, "res": {"plan": plan}, **fields}


def _copy_content(*lo_ids: str, **fields: Any) -> Dict[str, Any]:
    """A ``pangea.course_missions`` record as the client writes it."""
    return {
        "version": 1,
        "revision": 1,
        "missions": [{"lo_id": lo_id, "activity_ids": []} for lo_id in lo_ids],
        **fields,
    }


def _course_state(
    copy: Optional[Dict[str, Any]] = None,
    copy_sender: str = TEACHER,
    plan: Optional[Dict[str, Any]] = None,
    creator_power: bool = False,
) -> Dict[Tuple[str, str], Any]:
    """A course space's state: the teacher at 100, a student at the default,
    and the course plan and copy when given."""
    state: Dict[Tuple[str, str], Any] = {
        ("m.room.create", ""): SimpleNamespace(
            content={},
            sender=TEACHER,
            room_version=SimpleNamespace(msc4289_creator_power_enabled=creator_power),
        ),
        ("m.room.power_levels", ""): SimpleNamespace(
            content={"users": {TEACHER: 100}, "users_default": 0}, sender=TEACHER
        ),
    }
    if plan is not None:
        state[("pangea.course_plan", "")] = SimpleNamespace(
            content=plan, sender=TEACHER
        )
    if copy is not None:
        state[("pangea.course_missions", "")] = SimpleNamespace(
            content=copy, sender=copy_sender, room_id=COURSE
        )
    return state


class TestCourseCopyLoIds(unittest.TestCase):
    """The Missions a course's own Can-Do list names, read as the client
    reads it: a course admin's well-formed record, or nothing."""

    def test_shown_and_removed_missions_of_an_admins_copy(self):
        copy = _copy_content(
            "lo-1",
            "lo-added",
            removed_missions=[
                {"lo_id": "lo-gone", "activity_ids": ["a"], "shown_count": 1}
            ],
        )
        self.assertEqual(
            course_copy_lo_ids(_course_state(copy)), ["lo-1", "lo-added", "lo-gone"]
        )

    def test_a_copy_a_student_wrote_names_nothing(self):
        state = _course_state(_copy_content("lo-added"), copy_sender=_student(1))
        self.assertEqual(course_copy_lo_ids(state), [])

    def test_a_copy_whose_writer_is_no_longer_an_admin_names_nothing(self):
        state = _course_state(_copy_content("lo-added"))
        state[("m.room.power_levels", "")].content["users"][TEACHER] = 50
        self.assertEqual(course_copy_lo_ids(state), [])

    def test_a_room_version_12_creator_counts_as_an_admin(self):
        state = _course_state(_copy_content("lo-added"), creator_power=True)
        state[("m.room.power_levels", "")].content["users"] = {}
        self.assertEqual(course_copy_lo_ids(state), ["lo-added"])

    def test_no_copy_names_nothing(self):
        self.assertEqual(course_copy_lo_ids(_course_state()), [])

    def test_a_malformed_copy_names_nothing(self):
        for broken in (
            _copy_content("lo-1", version=2),
            _copy_content("lo-1", revision=0),
            _copy_content("lo-1", revision=True),
            _copy_content("lo-1", missions="lo-1"),
            _copy_content("lo-1", "lo-1"),
            _copy_content("lo-1", missions=[{"lo_id": "", "activity_ids": []}]),
            _copy_content("lo-1", missions=[{"lo_id": "lo-1"}]),
            _copy_content("lo-1", missions=[{"lo_id": "lo-1", "activity_ids": [1]}]),
            _copy_content(
                "lo-1",
                missions=[
                    {"lo_id": "lo-1", "activity_ids": [], "removed_activity_ids": "a"}
                ],
            ),
            _copy_content("lo-1", removed_missions={}),
            _copy_content(
                "lo-1",
                removed_missions=[
                    {"lo_id": "lo-2", "activity_ids": ["a"], "shown_count": 2}
                ],
            ),
        ):
            with self.subTest(broken=broken):
                with self.assertLogs(COURSE_MISSIONS_LOGGER, "WARNING"):
                    self.assertEqual(course_copy_lo_ids(_course_state(broken)), [])

    def test_the_empty_copy_a_failed_read_leaves_names_nothing_quietly(self):
        with self.assertNoLogs(COURSE_MISSIONS_LOGGER, "WARNING"):
            self.assertEqual(course_copy_lo_ids(_course_state({})), [])


class TestFetchPlan(unittest.IsolatedAsyncioTestCase):
    """Which activity a course may open as a discussion: its own copy, or a
    catalog discussion filed under a Mission of the course's quest."""

    def setUp(self) -> None:
        self.api = Mock()
        self.api.get_room_state = AsyncMock(
            return_value={
                ("pangea.course_plan", ""): SimpleNamespace(content={"uuid": "quest-1"})
            }
        )
        config = PangeaChatConfig(
            cms_base_url="http://cms.test",
            cms_service_api_key="key",
            user_activity_notification_bot_user_id=BOT,
            discussion_room_choreo_base_url="http://choreo.test",
        )
        self.resource = DiscussionRoom(self.api, config)
        self.quest_lo_ids = AsyncMock(return_value=["lo-1", "lo-2"])
        patcher = patch.object(self.resource._quests, "quest_lo_ids", self.quest_lo_ids)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cms(self, code: int, docs: Optional[List[Any]] = None) -> AsyncMock:
        body = json.dumps({"docs": docs} if docs is not None else {}).encode()
        cms = AsyncMock(return_value=(code, body))
        patcher = patch.object(discussion_room, "cms_request", cms)
        patcher.start()
        self.addCleanup(patcher.stop)
        return cms

    async def test_a_catalog_discussion_under_a_mission_of_the_quest(self):
        plan = _discussion_plan()
        cms = self._cms(200, [_row(plan, learningObjectiveRefs=["lo-2"])])
        self.assertEqual(await self.resource._fetch_plan(COURSE, ACTIVITY), plan)
        url = cms.await_args.args[2]
        self.assertIn("/api/activities-v2?", url)
        self.assertIn("limit=2", url)
        self.quest_lo_ids.assert_awaited_once_with("quest-1")

    async def test_a_catalog_discussion_outside_the_quest_is_not_found(self):
        self._cms(200, [_row(_discussion_plan(), learningObjectiveRefs=["lo-9"])])
        self.assertIsNone(await self.resource._fetch_plan(COURSE, ACTIVITY))

    async def test_a_course_with_no_quest_uses_no_catalog_discussion(self):
        self.api.get_room_state.return_value = {}
        self._cms(200, [_row(_discussion_plan(), learningObjectiveRefs=["lo-1"])])
        self.assertIsNone(await self.resource._fetch_plan(COURSE, ACTIVITY))
        self.quest_lo_ids.assert_not_awaited()

    async def test_an_older_space_naming_its_quest_as_course_plan_id(self):
        self.api.get_room_state.return_value = _course_state(
            plan={"course_plan_id": "quest-1"}
        )
        plan = _discussion_plan()
        self._cms(200, [_row(plan, learningObjectiveRefs=["lo-2"])])
        self.assertEqual(await self.resource._fetch_plan(COURSE, ACTIVITY), plan)
        self.quest_lo_ids.assert_awaited_once_with("quest-1")

    async def test_uuid_wins_over_course_plan_id_and_empty_is_absent(self):
        self.api.get_room_state.return_value = _course_state(
            plan={"uuid": "", "course_plan_id": "quest-old"}
        )
        self._cms(200, [_row(_discussion_plan(), learningObjectiveRefs=["lo-2"])])
        await self.resource._fetch_plan(COURSE, ACTIVITY)
        self.quest_lo_ids.assert_awaited_once_with("quest-old")
        self.quest_lo_ids.reset_mock()
        self.resource._plans.clear()
        self.api.get_room_state.return_value = _course_state(
            plan={"uuid": "quest-1", "course_plan_id": "quest-old"}
        )
        await self.resource._fetch_plan(COURSE, ACTIVITY)
        self.quest_lo_ids.assert_awaited_once_with("quest-1")

    async def test_a_mission_the_courses_own_list_adds(self):
        self.api.get_room_state.return_value = _course_state(
            copy=_copy_content("lo-1", "lo-added"), plan={"uuid": "quest-1"}
        )
        plan = _discussion_plan()
        self._cms(200, [_row(plan, learningObjectiveRefs=["lo-added"])])
        self.assertEqual(await self.resource._fetch_plan(COURSE, ACTIVITY), plan)
        # The copy answered: the quest is not read.
        self.quest_lo_ids.assert_not_awaited()
        state_filter = self.api.get_room_state.await_args.kwargs["event_filter"]
        self.assertIn(("pangea.course_missions", ""), state_filter)
        self.assertIn(("m.room.power_levels", ""), state_filter)
        self.assertIn(("m.room.create", ""), state_filter)

    async def test_a_mission_only_a_students_list_adds_is_not_the_courses(self):
        self.api.get_room_state.return_value = _course_state(
            copy=_copy_content("lo-added"),
            copy_sender=_student(1),
            plan={"uuid": "quest-1"},
        )
        self._cms(200, [_row(_discussion_plan(), learningObjectiveRefs=["lo-added"])])
        self.assertIsNone(await self.resource._fetch_plan(COURSE, ACTIVITY))

    async def test_a_quest_mission_the_list_leaves_out_is_still_the_courses(self):
        self.api.get_room_state.return_value = _course_state(
            copy=_copy_content("lo-added"), plan={"uuid": "quest-1"}
        )
        plan = _discussion_plan()
        self._cms(200, [_row(plan, learningObjectiveRefs=["lo-2"])])
        self.assertEqual(await self.resource._fetch_plan(COURSE, ACTIVITY), plan)

    async def test_a_course_with_a_list_and_no_quest(self):
        self.api.get_room_state.return_value = _course_state(
            copy=_copy_content("lo-added")
        )
        plan = _discussion_plan()
        self._cms(200, [_row(plan, learningObjectiveRefs=["lo-added"])])
        self.assertEqual(await self.resource._fetch_plan(COURSE, ACTIVITY), plan)
        self.resource._plans.clear()
        self._cms(200, [_row(plan, learningObjectiveRefs=["lo-9"])])
        self.assertIsNone(await self.resource._fetch_plan(COURSE, ACTIVITY))
        self.quest_lo_ids.assert_not_awaited()

    async def test_the_courses_own_copy_and_never_another_courses(self):
        plan = _discussion_plan()
        self._cms(200, [_row(plan, course_room_id=COURSE)])
        self.assertEqual(await self.resource._fetch_plan(COURSE, ACTIVITY), plan)
        self.assertIsNone(await self.resource._fetch_plan(OTHER_COURSE, ACTIVITY))
        self.quest_lo_ids.assert_not_awaited()

    async def test_an_activity_not_shaped_as_a_discussion_is_not_found(self):
        roleplay = _discussion_plan(mode="Roleplay")
        self._cms(200, [_row(roleplay, learningObjectiveRefs=["lo-1"])])
        self.assertIsNone(await self.resource._fetch_plan(COURSE, ACTIVITY))

    async def test_an_unknown_activity_is_not_found(self):
        self._cms(200, [])
        self.assertIsNone(await self.resource._fetch_plan(COURSE, ACTIVITY))

    async def test_failures_to_ask_raise(self):
        self._cms(500, [])
        with self.assertRaises(ValueError):
            await self.resource._fetch_plan(COURSE, ACTIVITY)

    async def test_two_rows_for_one_activity_raise(self):
        row = _row(_discussion_plan(), learningObjectiveRefs=["lo-1"])
        self._cms(200, [row, row])
        with self.assertRaises(ValueError):
            await self.resource._fetch_plan(COURSE, ACTIVITY)

    async def test_a_row_without_a_plan_raises(self):
        self._cms(200, [{"id": "row-1", "res": {}}])
        with self.assertRaises(ValueError):
            await self.resource._fetch_plan(COURSE, ACTIVITY)


# --- Placement and the preview ---------------------------------------------------

SIGNATURE = "0123456789abcdef0123456789abcdef"


def _course_room_state(
    *, space: bool = True, plan: bool = True, membership: str = "join"
) -> Dict[Tuple[str, str], Any]:
    state: Dict[Tuple[str, str], Any] = {
        ("m.room.create", ""): SimpleNamespace(
            content={"type": "m.space"} if space else {}, sender=TEACHER
        ),
        ("m.room.member", _student(1)): _member(_student(1), membership),
    }
    if plan:
        state[("pangea.course_plan", "")] = SimpleNamespace(
            content={"uuid": "quest-1"}, sender=TEACHER
        )
    return state


class TestPlacement(unittest.IsolatedAsyncioTestCase):
    """Which room a tap reaches, decided from the rooms' state: refusals of a
    removed caller, an own room without its bot, stale seats, the course
    check and the version check."""

    def setUp(self) -> None:
        self.api = Mock()
        config = PangeaChatConfig(
            cms_base_url="http://cms.test",
            cms_service_api_key="key",
            user_activity_notification_bot_user_id=BOT,
            discussion_room_choreo_base_url="http://choreo.test",
        )
        self.resource = DiscussionRoom(self.api, config)
        self.resource._clock = Mock(time=Mock(return_value=1000.0))
        self.views: List[Any] = []
        for name, mock in (
            ("_room_views", AsyncMock(side_effect=lambda *_: self.views)),
            ("_create", AsyncMock(return_value="!new:example.com")),
            ("_send_roles", AsyncMock()),
        ):
            patcher = patch.object(self.resource, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.room_state: Dict[Tuple[str, str], Any] = {}
        self.api.get_room_state = AsyncMock(
            side_effect=lambda *_a, **_k: self.room_state
        )

    def _view(
        self,
        room_id: str,
        members: Dict[str, Any],
        roles: Any,
        bot: bool = True,
        joining=frozenset(),
    ) -> Any:
        members = dict(members)
        members[BOT] = _member(BOT, "join" if bot else "leave", sender=TEACHER)
        return room_view(room_id, _state(members, roles), BOT, 0, joining)

    async def _reserve(self, version: Optional[_Version] = None) -> Any:
        return await self.resource._reserve(
            _student(1),
            COURSE,
            ACTIVITY,
            _discussion_plan(),
            version or _Version(SIGNATURE),
            None,
            BOT,
            set(),
        )

    async def test_a_removed_or_banned_caller_is_refused_by_join_and_preview(self):
        other = "!other:example.com"
        for member in (
            _member(_student(1), "leave", sender=TEACHER, prev="join"),
            _member(_student(1), "ban", sender=TEACHER),
        ):
            with self.subTest(membership=member.content["membership"]):
                self.resource._views.clear()
                self.views = [
                    self._view(ROOM, {_student(1): member}, _roles(_student(1))),
                    self._view(other, {}, _roles()),
                ]
                with self.assertRaises(_Refused) as join:
                    await self._reserve()
                with self.assertRaises(_Refused) as preview:
                    await self.resource._preview(_student(1), COURSE, ACTIVITY, BOT)
                for refused in (join.exception, preview.exception):
                    self.assertEqual(refused.status, 403)
                    self.assertEqual(refused.body["errcode"], ERRCODE_REMOVED)
                self.resource._create.assert_not_awaited()
                self.resource._send_roles.assert_not_awaited()

    async def test_an_unbanned_caller_returns_to_their_own_room(self):
        member = _member(_student(1), "leave", sender=TEACHER, prev="ban")
        self.views = [self._view(ROOM, {_student(1): member}, _roles(_student(1)))]
        self.room_state = {(ACTIVITY_ROLES_EVENT, ""): _roles(_student(1))}
        reservation = await self._reserve()
        self.assertEqual((reservation.room_id, reservation.own), (ROOM, True))

    async def test_an_own_room_without_its_bot_is_skipped(self):
        member = _member(_student(1), "join")
        self.views = [
            self._view(ROOM, {_student(1): member}, _roles(_student(1)), bot=False)
        ]
        with self.assertLogs(discussion_room.logger, "WARNING"):
            reservation = await self._reserve()
        self.assertEqual(reservation.room_id, "!new:example.com")
        self.assertTrue(reservation.created)
        with self.assertLogs(discussion_room.logger, "WARNING"):
            preview = await self.resource._preview(_student(1), COURSE, ACTIVITY, BOT)
        self.assertEqual(preview, {"room_id": None, "students": 0, "joined": False})

    async def test_a_seat_write_drops_the_rooms_stale_seats(self):
        stale, joined = _student(7), _student(8)
        roles = _roles(stale, joined)
        self.views = [
            self._view(ROOM, {joined: _member(joined, "join")}, roles),
        ]
        self.room_state = {(ACTIVITY_ROLES_EVENT, ""): roles}
        reservation = await self._reserve()
        self.assertEqual((reservation.room_id, reservation.wrote_seat), (ROOM, True))
        content = self.resource._send_roles.await_args.args[1]
        self.assertEqual(seat_holders(content), {joined, _student(1)})

    async def test_a_stale_holder_who_joined_since_keeps_their_seat(self):
        stale = _student(7)
        roles = _roles(stale)
        self.views = [self._view(ROOM, {}, roles)]
        self.room_state = {
            (ACTIVITY_ROLES_EVENT, ""): roles,
            ("m.room.member", stale): _member(stale, "join"),
        }
        await self._reserve()
        content = self.resource._send_roles.await_args.args[1]
        self.assertEqual(seat_holders(content), {stale, _student(1)})

    async def test_an_unknown_version_refuses_only_a_new_room(self):
        refusal = _Refused(409, "changed", ERRCODE_UNKNOWN_VERSION)
        with self.assertRaises(_Refused):
            await self._reserve(_Version(None, refusal))
        self.resource._create.assert_not_awaited()
        # A room with space takes the caller whatever the version.
        self.views = [self._view(ROOM, {}, _roles())]
        self.room_state = {(ACTIVITY_ROLES_EVENT, ""): _roles()}
        reservation = await self._reserve(_Version(None, refusal))
        self.assertEqual(reservation.room_id, ROOM)

    async def test_the_course_must_be_a_course_space_the_caller_is_in(self):
        self.room_state = _course_room_state()
        await self.resource._check_course(_student(1), COURSE)
        for state, error in (
            (_course_room_state(membership="leave"), "Not a member of this course"),
            ({}, "Not a member of this course"),
            (_course_room_state(space=False), "Not a course"),
            (_course_room_state(plan=False), "Not a course"),
        ):
            with self.subTest(error=error, keys=sorted(state)):
                self.room_state = state
                with self.assertRaises(_Refused) as refused:
                    await self.resource._check_course(_student(1), COURSE)
                self.assertEqual(refused.exception.status, 403)
                self.assertEqual(refused.exception.body["error"], error)

    async def test_the_version_is_checked_with_choreo(self):
        knows = AsyncMock(return_value=True)
        with patch.object(self.resource, "_choreo_knows", knows):
            self.assertEqual(
                await self.resource._checked_version(SIGNATURE, ACTIVITY, "tok"),
                _Version(SIGNATURE),
            )
            # Known versions are reused for a while.
            await self.resource._checked_version(SIGNATURE, ACTIVITY, "tok")
            knows.assert_awaited_once_with(ACTIVITY, SIGNATURE, "tok")
            knows.return_value = False
            unknown = await self.resource._checked_version("f" * 32, ACTIVITY, "tok")
        self.assertIsNone(unknown.pin)
        assert isinstance(unknown.refusal, _Refused)
        self.assertEqual(unknown.refusal.status, 409)
        self.assertEqual(unknown.refusal.body["errcode"], ERRCODE_UNKNOWN_VERSION)

    async def test_a_choreo_that_cannot_be_asked_refuses_a_new_room_as_502(self):
        down = AsyncMock(side_effect=ConnectionError("down"))
        with patch.object(self.resource, "_choreo_knows", down):
            version = await self.resource._checked_version(SIGNATURE, ACTIVITY, "t")
        self.assertIsInstance(version.refusal, ChoreoUnavailable)

    async def test_no_version_or_no_choreo_follows_the_latest(self):
        with self.assertLogs(discussion_room.logger, "WARNING"):
            self.assertEqual(
                await self.resource._checked_version(None, ACTIVITY, "t"),
                _Version(None),
            )
        self.resource._choreo_base_url = None
        with self.assertLogs(discussion_room.logger, "WARNING"):
            self.assertEqual(
                await self.resource._checked_version(SIGNATURE, ACTIVITY, "t"),
                _Version(None),
            )

    async def test_choreo_knows_a_version_it_serves_as_itself(self):
        for body, known in (
            ({"version_id": SIGNATURE, "used_fallback_version": False}, True),
            ({"version_id": "f" * 32, "used_fallback_version": True}, False),
            ({"version_id": SIGNATURE, "used_fallback_version": True}, False),
        ):
            cms = AsyncMock(return_value=(200, json.dumps(body).encode()))
            with self.subTest(body=body), patch.object(
                discussion_room, "cms_request", cms
            ):
                self.assertEqual(
                    await self.resource._choreo_knows(ACTIVITY, SIGNATURE, "tok"),
                    known,
                )
                url = cms.await_args.args[2]
                self.assertEqual(
                    url,
                    f"http://choreo.test/choreo/v2/activity/{ACTIVITY}"
                    f"?version={SIGNATURE}",
                )
                headers = cms.await_args.args[3]
                self.assertEqual(headers.getRawHeaders("Authorization"), ["Bearer tok"])
        cms = AsyncMock(return_value=(503, b"{}"))
        with patch.object(discussion_room, "cms_request", cms):
            with self.assertRaises(ValueError):
                await self.resource._choreo_knows(ACTIVITY, SIGNATURE, "tok")


# --- Direct joins ------------------------------------------------------------------


class TestDirectJoinCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.api = Mock()
        self.create = SimpleNamespace(
            sender=BOT, content=creation_content(COURSE, ACTIVITY)
        )
        self.api._hs.get_storage_controllers().state.get_current_state_event = (
            AsyncMock(side_effect=lambda *_: self.create)
        )
        self.room_state: Dict[Tuple[str, str], Any] = {}
        self.read_state = AsyncMock(side_effect=lambda *_a, **_k: self.room_state)
        state_patcher = patch.object(
            join_check, "room_state_with_prev", self.read_state
        )
        state_patcher.start()
        self.addCleanup(state_patcher.stop)
        self.check = DiscussionJoinCheck(self.api, BOT)
        self.admins = AsyncMock(return_value=frozenset({TEACHER}))
        patcher = patch.object(join_check, "course_admins", self.admins)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _may_join(self, user_id: str, invited: bool = False) -> Any:
        return await self.check.user_may_join_room(user_id, ROOM, invited)

    async def test_a_seat_holder_may_join(self):
        self.room_state = {(ACTIVITY_ROLES_EVENT, ""): _roles(_student(1))}
        self.assertEqual(await self._may_join(_student(1)), NOT_SPAM)

    async def test_anyone_without_a_seat_is_refused(self):
        self.room_state = {(ACTIVITY_ROLES_EVENT, ""): _roles(_student(1))}
        self.assertEqual(await self._may_join(_student(2)), Codes.FORBIDDEN)

    async def test_a_removed_seat_holder_is_refused(self):
        self.room_state = {
            (ACTIVITY_ROLES_EVENT, ""): _roles(_student(1)),
            ("m.room.member", _student(1)): _member(_student(1), "leave", TEACHER),
        }
        self.assertEqual(await self._may_join(_student(1)), Codes.FORBIDDEN)

    async def test_an_unbanned_seat_holder_may_come_back(self):
        self.room_state = {
            (ACTIVITY_ROLES_EVENT, ""): _roles(_student(1)),
            ("m.room.member", _student(1)): _member(
                _student(1), "leave", TEACHER, prev="ban"
            ),
        }
        self.assertEqual(await self._may_join(_student(1)), NOT_SPAM)

    async def test_a_seat_holder_who_left_on_their_own_may_come_back(self):
        self.room_state = {
            (ACTIVITY_ROLES_EVENT, ""): _roles(_student(1)),
            ("m.room.member", _student(1)): _member(_student(1), "leave"),
        }
        self.assertEqual(await self._may_join(_student(1)), NOT_SPAM)

    async def test_course_admins_the_bot_and_invited_users_may_join(self):
        self.assertEqual(await self._may_join(TEACHER), NOT_SPAM)
        self.assertEqual(await self._may_join(BOT), NOT_SPAM)
        self.assertEqual(await self._may_join(_student(3), invited=True), NOT_SPAM)

    async def test_other_rooms_are_not_checked(self):
        self.create = SimpleNamespace(
            sender=_student(1), content=creation_content(COURSE, ACTIVITY)
        )
        self.assertEqual(await self._may_join(_student(2)), NOT_SPAM)
        self.create = None
        self.assertEqual(await self._may_join(_student(2)), NOT_SPAM)
        self.admins.assert_not_awaited()

    async def test_a_broken_check_fails_open_and_is_captured(self):
        self.read_state.side_effect = RuntimeError("state unavailable")
        with patch.object(join_check, "capture_exception") as capture, self.assertLogs(
            join_check.logger, "ERROR"
        ):
            self.assertEqual(await self._may_join(_student(2)), NOT_SPAM)
        capture.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestYoutubeId(unittest.TestCase):
    """The room image's video id is read only from YouTube's own hosts."""

    def test_youtube_hosts(self) -> None:
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        ):
            with self.subTest(url=url):
                self.assertEqual(discussion_room._youtube_id(url), "dQw4w9WgXcQ")

    def test_lookalike_hosts_are_refused(self) -> None:
        for url in (
            "https://evilyoutube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com.example.org/watch?v=dQw4w9WgXcQ",
        ):
            with self.subTest(url=url):
                self.assertIsNone(discussion_room._youtube_id(url))
