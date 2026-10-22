import unittest

from synapse_pangea_chat.course_default_chats.course_default_chats import (
    ChatRecoveryPlan,
    ChatState,
    RecoveryRefused,
    SpaceRoster,
    admins_to_demote,
    admins_to_promote,
    is_default_chat_alias,
    owned_by_space_admin,
    plan_recovery,
)

TEACHER = "@teacher:example.com"
CO_TEACHER = "@co:example.com"
STUDENT = "@student:example.com"
OUTSIDER = "@outsider:example.com"


class TestIsDefaultChatAlias(unittest.TestCase):
    def test_the_client_prefixes_on_the_localpart(self):
        self.assertTrue(is_default_chat_alias("#announcementsChat_abc_1:example.com"))
        self.assertTrue(is_default_chat_alias("#introductionChat_abc_1:example.com"))

    def test_other_aliases_and_shapes_are_not(self):
        self.assertFalse(is_default_chat_alias("#general:example.com"))
        self.assertFalse(is_default_chat_alias("#myannouncementsChat:example.com"))
        self.assertFalse(is_default_chat_alias("announcementsChat_abc:example.com"))
        self.assertFalse(is_default_chat_alias(None))
        self.assertFalse(is_default_chat_alias(42))


class TestAdminMirror(unittest.TestCase):
    def test_space_admins_below_admin_power_are_promoted(self):
        users = {TEACHER: 100, CO_TEACHER: 50}
        self.assertEqual(
            admins_to_promote(users, 0, frozenset({TEACHER, CO_TEACHER}), frozenset()),
            [CO_TEACHER],
        )

    def test_a_creator_with_immutable_power_is_left_alone(self):
        self.assertEqual(
            admins_to_promote({}, 0, frozenset({TEACHER}), frozenset({TEACHER})), []
        )

    def test_joined_members_who_lost_space_admin_are_demoted(self):
        users = {TEACHER: 100, CO_TEACHER: 100, STUDENT: 50, OUTSIDER: 100}
        demoted = admins_to_demote(
            users,
            0,
            chat_joined=frozenset({TEACHER, CO_TEACHER, STUDENT, OUTSIDER}),
            space_members=frozenset({TEACHER, CO_TEACHER, STUDENT}),
            space_admins=frozenset({TEACHER}),
            immutable=frozenset(),
        )
        # The co-teacher lost admin on the space. The student was never an
        # admin, and the outsider is not a space member, so neither is touched.
        self.assertEqual(demoted, [CO_TEACHER])

    def test_a_member_who_left_the_chat_is_not_demoted(self):
        users = {CO_TEACHER: 100}
        demoted = admins_to_demote(
            users,
            0,
            chat_joined=frozenset(),
            space_members=frozenset({CO_TEACHER}),
            space_admins=frozenset(),
            immutable=frozenset(),
        )
        self.assertEqual(demoted, [])

    def test_non_integer_power_reads_as_default(self):
        self.assertEqual(
            admins_to_promote({TEACHER: "lots"}, 0, frozenset({TEACHER}), frozenset()),
            [TEACHER],
        )


class TestChatState(unittest.TestCase):
    def test_reads_users_default_and_joined(self):
        state = ChatState(
            power_content={"users": {TEACHER: 100}, "users_default": 10},
            memberships={TEACHER: "join", STUDENT: "leave"},
            immutable=frozenset(),
        )
        self.assertEqual(state.users, {TEACHER: 100})
        self.assertEqual(state.users_default, 10)
        self.assertEqual(state.joined, frozenset({TEACHER}))

    def test_missing_power_levels_read_as_empty(self):
        state = ChatState(power_content={}, memberships={}, immutable=frozenset())
        self.assertEqual(state.users, {})
        self.assertEqual(state.users_default, 0)


if __name__ == "__main__":
    unittest.main()


class TestOwnedBySpaceAdmin(unittest.TestCase):
    def _chat(self, creator, users):
        return ChatState(
            power_content={"users": users},
            memberships={},
            immutable=frozenset(),
            creator=creator,
        )

    def test_a_chat_a_space_admin_created_is_theirs(self):
        chat = self._chat(TEACHER, {TEACHER: 100})
        self.assertTrue(owned_by_space_admin(chat, frozenset({TEACHER})))

    def test_a_chat_a_space_admin_already_runs_is_theirs(self):
        chat = self._chat(STUDENT, {STUDENT: 100, CO_TEACHER: 100})
        self.assertTrue(owned_by_space_admin(chat, frozenset({CO_TEACHER})))

    def test_a_students_prefixed_room_is_not(self):
        chat = self._chat(STUDENT, {STUDENT: 100})
        self.assertFalse(owned_by_space_admin(chat, frozenset({TEACHER})))

    def test_a_creator_who_stopped_being_an_admin_does_not_count(self):
        chat = self._chat(CO_TEACHER, {CO_TEACHER: 100})
        self.assertFalse(owned_by_space_admin(chat, frozenset({TEACHER})))


def _is_local(user_id):
    return user_id.endswith(":example.com")


class TestPlanRecovery(unittest.TestCase):
    """The dead chat: its creator (a space admin) left, and the members still
    in it hold no power, so nobody can let anyone in."""

    def _dead_chat(self, memberships=None, users=None, creator=TEACHER):
        return ChatState(
            power_content={
                "users": {TEACHER: 100} if users is None else users,
                "invite": 50,
            },
            memberships=(
                {TEACHER: "leave", STUDENT: "join"}
                if memberships is None
                else memberships
            ),
            immutable=frozenset(),
            creator=creator,
        )

    def _roster(self, admins=frozenset({TEACHER, CO_TEACHER})):
        return SpaceRoster(members=admins | {STUDENT}, admins=admins)

    def _refusal(self, chat, user_id, roster=None):
        with self.assertRaises(RecoveryRefused) as raised:
            plan_recovery(chat, roster or self._roster(), user_id, _is_local)
        return raised.exception.reason

    def test_an_admin_who_left_but_still_holds_power_needs_only_the_join(self):
        plan = plan_recovery(self._dead_chat(), self._roster(), TEACHER, _is_local)
        self.assertEqual(plan, ChatRecoveryPlan(join=True, promote=False))

    def test_another_space_admin_needs_the_join_and_the_promotion(self):
        plan = plan_recovery(self._dead_chat(), self._roster(), CO_TEACHER, _is_local)
        self.assertEqual(plan, ChatRecoveryPlan(join=True, promote=True))

    def test_a_space_admin_already_in_the_chat_without_power_is_only_promoted(self):
        chat = self._dead_chat(memberships={CO_TEACHER: "join", STUDENT: "join"})
        plan = plan_recovery(chat, self._roster(), CO_TEACHER, _is_local)
        self.assertEqual(plan, ChatRecoveryPlan(join=False, promote=True))

    def test_a_chat_someone_can_still_let_members_in_goes_to_the_repair(self):
        chat = self._dead_chat(users={TEACHER: 100, STUDENT: 50})
        self.assertEqual(self._refusal(chat, CO_TEACHER), "chat_has_admin")

    def test_only_a_space_admin_can_be_restored(self):
        self.assertEqual(self._refusal(self._dead_chat(), STUDENT), "not_space_admin")

    def test_a_banned_space_admin_is_not_let_back_in(self):
        chat = self._dead_chat(memberships={CO_TEACHER: "ban", STUDENT: "join"})
        self.assertEqual(self._refusal(chat, CO_TEACHER), "banned")

    def test_a_chat_with_members_on_another_server_is_refused(self):
        chat = self._dead_chat(
            memberships={STUDENT: "join", "@guest:elsewhere.org": "join"}
        )
        self.assertEqual(self._refusal(chat, TEACHER), "remote_members")

    def test_a_room_that_is_not_the_space_admins_own_is_refused(self):
        chat = self._dead_chat(users={}, creator=STUDENT)
        self.assertEqual(self._refusal(chat, TEACHER), "not_a_default_chat")

    def test_a_chat_with_no_current_state_is_refused(self):
        chat = ChatState(power_content={}, memberships={}, immutable=frozenset())
        self.assertEqual(self._refusal(chat, TEACHER), "no_current_state")
