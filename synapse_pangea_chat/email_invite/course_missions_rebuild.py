"""Builds a claimed course's Can-Do list again after the claim, when the
claim could not build it whole.

The claim bounds its CMS read (course_missions.FETCH_TIMEOUT_SECONDS) and
creates the course either way. Two outcomes are built again here, after
``REBUILD_DELAYS_SECONDS``:

- The read failed for a reason a later attempt can get past: the course has
  no list and reads the shared quest. The list is written only while the
  course still has none of its own.
- The read finished, but some Can-Dos' ranked rows were not read in time, so
  their pins were ranked from the newest library page. The list the claim
  wrote (``expected``) is replaced, one revision up, only while the course
  still holds exactly that list.

Either way a list the teacher has published since is never overwritten, and
nothing is written once the course is on another quest: a teacher who changed
course meanwhile may have been left with no list by the app, and the old
quest's Can-Dos must not land in the new course. A
rebuild has no one waiting on it, so its read gets ``REBUILD_BUDGET_SECONDS``.
The list is sent by the course's strongest joined local instructor, as
set_course_plan sends one: readers ignore a list sent below power level 100.

The attempts are timers in this process: a restart before they finish drops
them. The claim's capture is what tells an operator to build the list
(scripts/write_course_copies.py) if no later attempt does.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from synapse.util.duration import Duration

from synapse_pangea_chat.email_invite.course_missions import (
    COURSE_MISSIONS_STATE_EVENT_TYPE,
    RETRYABLE,
    CourseMissionsError,
    CourseMissionsReader,
)
from synapse_pangea_chat.email_invite.course_space_defaults import (
    PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
)
from synapse_pangea_chat.public_courses.get_public_courses import extract_plan_id
from synapse_pangea_chat.room_code.instructor_access import joined_local_instructors
from synapse_pangea_chat.sentry_capture import capture_message

REBUILD_DELAYS_SECONDS = (60, 600, 3600)
REBUILD_BUDGET_SECONDS = 30

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.course_missions_rebuild"
)


def _plain(value: Any) -> Any:
    """A plain copy of room state content, which Synapse freezes (lists as
    tuples), so it compares equal to the content that was written."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _list_content(state: Mapping[Any, Any]) -> Optional[Dict[str, Any]]:
    """The course's list, or None when it has none. An empty list is what a
    failed read used to write: none."""
    event = state.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
    if event is None or not event.content:
        return None
    return _plain(event.content)


def _on_quest(state: Mapping[Any, Any], quest_id: str) -> bool:
    event = state.get((PANGEA_COURSE_PLAN_STATE_EVENT_TYPE, ""))
    content = event.content if event is not None else None
    return isinstance(content, Mapping) and extract_plan_id(content) == quest_id


def _may_write(
    state: Mapping[Any, Any], quest_id: str, expected: Optional[Mapping[str, Any]]
) -> bool:
    """Still the course the rebuild was for: on the same quest, with no list
    of its own, or exactly the list the claim wrote."""
    if not _on_quest(state, quest_id):
        return False
    current = _list_content(state)
    return current is None or (expected is not None and current == expected)


class CourseMissionsRebuild:
    def __init__(self, api: Any, reader: CourseMissionsReader) -> None:
        self._api = api
        self._reader = reader
        self._clock = api._hs.get_clock()

    def schedule(
        self,
        room_id: str,
        quest_id: str,
        attempt: int = 0,
        expected: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._clock.call_later(
            Duration(seconds=REBUILD_DELAYS_SECONDS[attempt]),
            self._api.run_as_background_process,
            "course_missions_rebuild",
            self.attempt,
            room_id,
            quest_id,
            attempt,
            expected,
        )

    def _give_up(
        self, why: str, room_id: str, quest_id: str, attempt: int, **extra: Any
    ) -> None:
        logger.error(
            "Can-Do list for %s (quest %s) was not built after the claim: %s %s",
            room_id,
            quest_id,
            why,
            extra,
        )
        capture_message(
            f"Can-Do list not built after the claim: {why}",
            level="error",
            tags={"course_missions.rebuild": why},
            context={
                "room_id": room_id,
                "quest_id": quest_id,
                "attempts": attempt + 1,
                **extra,
            },
        )

    async def attempt(
        self,
        room_id: str,
        quest_id: str,
        attempt: int,
        expected: Optional[Mapping[str, Any]] = None,
    ) -> None:
        last = attempt + 1 >= len(REBUILD_DELAYS_SECONDS)
        try:
            await self._attempt(room_id, quest_id, attempt, expected, last)
        except Exception as e:
            # Not the CMS (that is handled inside): Synapse itself failed.
            if last:
                self._give_up(
                    "unexpected_error",
                    room_id,
                    quest_id,
                    attempt,
                    error=type(e).__name__,
                )
            else:
                logger.exception(
                    "Can-Do list rebuild for %s failed; trying again later", room_id
                )
                self.schedule(room_id, quest_id, attempt + 1, expected)

    async def _attempt(
        self,
        room_id: str,
        quest_id: str,
        attempt: int,
        expected: Optional[Mapping[str, Any]],
        last: bool,
    ) -> None:
        before = await self._api.get_room_state(
            room_id,
            [
                (COURSE_MISSIONS_STATE_EVENT_TYPE, ""),
                (PANGEA_COURSE_PLAN_STATE_EVENT_TYPE, ""),
            ],
        )
        if not _may_write(before, quest_id, expected):
            logger.info(
                "Can-Do list for %s: the course or its list changed since the claim",
                room_id,
            )
            return
        try:
            built = await self._reader.read(
                quest_id, budget=REBUILD_BUDGET_SECONDS, report=False
            )
        except CourseMissionsError as e:
            if last or e.code not in RETRYABLE:
                self._give_up(e.code, room_id, quest_id, attempt, detail=e.detail)
            else:
                logger.warning(
                    "Can-Do list for %s: attempt %d failed (%s); trying again",
                    room_id,
                    attempt + 1,
                    e,
                )
                self.schedule(room_id, quest_id, attempt + 1, expected)
            return
        if built.unfinished and not last:
            logger.warning(
                "Can-Do list for %s: %d Can-Dos still not read; trying again",
                room_id,
                len(built.unfinished),
            )
            self.schedule(room_id, quest_id, attempt + 1, expected)
            return
        # The read takes seconds: a list the teacher published meanwhile wins.
        state = await self._api.get_room_state(room_id)
        if not _may_write(state, quest_id, expected):
            logger.info(
                "Can-Do list for %s: the course or its list changed during the "
                "rebuild",
                room_id,
            )
            return
        current = _list_content(state)
        instructors = joined_local_instructors(self._api, state)
        if not instructors:
            self._give_up("no_sender", room_id, quest_id, attempt)
            return
        content = dict(built.content)
        if current is not None:
            # One revision up, so a Studio opened on the claim's list refuses
            # its stale publish.
            revision = current.get("revision")
            content["revision"] = (revision if isinstance(revision, int) else 0) + 1
        await self._api.create_and_send_event_into_room(
            {
                "type": COURSE_MISSIONS_STATE_EVENT_TYPE,
                "state_key": "",
                "room_id": room_id,
                "sender": instructors[0],
                "content": content,
            }
        )
        if built.unfinished:
            self._give_up(
                "still_unfinished",
                room_id,
                quest_id,
                attempt,
                unfinished=len(built.unfinished),
            )
        logger.info(
            "Can-Do list for %s (quest %s) written after the claim, attempt %d",
            room_id,
            quest_id,
            attempt + 1,
        )
