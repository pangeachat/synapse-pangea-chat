"""A managed student's moderation incidents reach their Protected courses
(pangeachat/.github#799, trust-and-safety "Protected courses"): the
recorder files each one under the chat's own courses and under every
Protected course managing the sender, whichever chat it was sent in. The
same against a real homeserver is in ``test_protected_courses_e2e.py``."""

from __future__ import annotations

import logging
import unittest
from typing import Any, Dict, Optional, Tuple, cast

from synapse_pangea_chat.moderation.incidents import (
    ACTION_REDACTED,
    SOURCE_MODERATION,
    Incident,
)
from synapse_pangea_chat.moderation.recorder import IncidentRecorder

STUDENT = "@student:hs"
OTHER = "@other:hs"
PROTECTED = "!protected:hs"
PLAIN = "!plain:hs"
PROTECTED_CHAT = "!protected-chat:hs"
PLAIN_CHAT = "!plain-chat:hs"
DM = "!dm:hs"

OWNERS: Dict[str, Tuple[str, ...]] = {
    PROTECTED_CHAT: (PROTECTED,),
    PLAIN_CHAT: (PLAIN,),
    DM: (),
}


class _Courses:
    def position_now(self) -> int:
        return 7

    async def for_room(self, room_id: str, position: int) -> Tuple[str, ...]:
        return OWNERS[room_id]


class _Names:
    async def label(self, room_id: str, subject_id: Any, position: int) -> str:
        return "Chat"


async def _managing(user_id: str) -> Tuple[str, ...]:
    return (PROTECTED,) if user_id == STUDENT else ()


async def _broken(user_id: str) -> Tuple[str, ...]:
    raise RuntimeError("down")


def _incident(room_id: str, sender: Optional[str] = STUDENT) -> Incident:
    return Incident(
        incident_id="mod:$e",
        source=SOURCE_MODERATION,
        action=ACTION_REDACTED,
        outcome=None,
        subject_id=sender,
        reporter_id=None,
        room_id=room_id,
        event_id="$e",
        course_ids=None,
        categories=("harassment",),
        self_harm=False,
        rule=None,
        top_score=0.9,
        text="text",
        reason=None,
        created_ms=1,
        updated_ms=1,
        as_of=5,
    )


class ManagedFlagsRouteTest(unittest.IsolatedAsyncioTestCase):
    def recorder(self, managing: Any = _managing) -> IncidentRecorder:
        recorder = IncidentRecorder(
            None, cast(Any, None), cast(Any, _Courses()), room_names=cast(Any, _Names())
        )
        recorder.managing_courses = managing
        return recorder

    async def courses(self, room_id: str, **kw: Any) -> Any:
        sender = kw.pop("sender", STUDENT)
        resolved = await self.recorder(**kw).resolve(_incident(room_id, sender))
        return resolved.course_ids

    async def test_a_managed_students_flag_reaches_the_protected_course(
        self,
    ) -> None:
        self.assertEqual(await self.courses(DM), (PROTECTED,))
        self.assertEqual(await self.courses(PLAIN_CHAT), (PLAIN, PROTECTED))

    async def test_the_managing_courses_own_chat_is_filed_once(self) -> None:
        self.assertEqual(await self.courses(PROTECTED_CHAT), (PROTECTED,))

    async def test_anyone_else_keeps_own_chat_routing(self) -> None:
        self.assertEqual(await self.courses(DM, sender=OTHER), ())
        self.assertEqual(await self.courses(PLAIN_CHAT, sender=OTHER), (PLAIN,))

    async def test_with_protected_courses_off_nothing_changes(self) -> None:
        self.assertEqual(await self.courses(DM, managing=None), ())
        self.assertEqual(await self.courses(PLAIN_CHAT, managing=None), (PLAIN,))

    async def test_a_failed_lookup_keeps_the_chats_courses_and_logs(self) -> None:
        with self.assertLogs(
            "synapse.modules.synapse_pangea_chat.moderation.recorder", logging.ERROR
        ) as logs:
            self.assertEqual(await self.courses(PLAIN_CHAT, managing=_broken), (PLAIN,))
        self.assertNotIn(STUDENT, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
