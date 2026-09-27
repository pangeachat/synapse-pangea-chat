from types import SimpleNamespace
from unittest import TestCase

from synapse_pangea_chat.room_code.instructor_access import is_joined_instructor


class TestInstructorAccess(TestCase):
    def state(self, creator_power, membership="join", power=0):
        return {
            ("m.room.create", ""): SimpleNamespace(
                sender="@teacher:x",
                content={},
                room_version=SimpleNamespace(
                    msc4289_creator_power_enabled=creator_power
                ),
            ),
            ("m.room.member", "@teacher:x"): SimpleNamespace(
                content={"membership": membership}
            ),
            ("m.room.power_levels", ""): SimpleNamespace(
                content={"users": {"@teacher:x": power}}
            ),
        }

    def test_ordinary_room_demotion_removes_authority(self):
        self.assertTrue(
            is_joined_instructor(self.state(False, power=100), "@teacher:x")
        )
        self.assertFalse(is_joined_instructor(self.state(False), "@teacher:x"))

    def test_creator_power_requires_joined_membership(self):
        self.assertTrue(is_joined_instructor(self.state(True), "@teacher:x"))
        for membership in ("leave", "ban", "invite"):
            self.assertFalse(
                is_joined_instructor(self.state(True, membership), "@teacher:x")
            )
