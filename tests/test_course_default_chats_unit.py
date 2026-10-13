import unittest

from synapse_pangea_chat.course_default_chats.course_default_chats import (
    ChatState,
    admins_to_demote,
    admins_to_promote,
    is_default_chat_alias,
    owned_by_space_admin,
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
