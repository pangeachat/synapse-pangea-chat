"""What every requested course space is created with.

The claim creates the space (provision_course.py) from these: the power levels
the client gives a course space it creates itself, and the course plan state
the public catalog reads. See create-course-space.instructions.md.
"""

from __future__ import annotations

from typing import Any, Dict

from synapse_pangea_chat.email_invite.course_missions import (
    COURSE_MISSIONS_STATE_EVENT_TYPE,
)

# Matches what the client creates a course space with (selected_course_page
# passes spaceChild: 0 into its defaultSpacePowerLevelsContent). m.space.child
# is 0 so a regular member can attach a room: learners' activity sessions fan
# out into their courses as space children, and a member sits at users_default.
DEFAULT_SPACE_POWER_LEVELS: Dict[str, Any] = {
    "ban": 50,
    "kick": 50,
    "invite": 50,
    "redact": 50,
    "events": {
        "m.room.power_levels": 100,
        "m.room.join_rules": 100,
        "m.space.child": 0,
        # The course space's Can-Do list (Content Studio): only a course admin
        # may write it.
        COURSE_MISSIONS_STATE_EVENT_TYPE: 100,
    },
    "events_default": 0,
    "state_default": 50,
    "users_default": 0,
    "notifications": {"room": 50},
}

PANGEA_COURSE_PLAN_STATE_EVENT_TYPE = "pangea.course_plan"


def build_course_plan_content(
    course_plan_id: Any, target_language: Any
) -> Dict[str, Any]:
    """Content for the ``pangea.course_plan`` state event of a new space.

    The plan id goes under ``uuid`` and, when the caller knows it, the course's
    target language under ``l2``. Both are read by the public course catalog —
    a space with no ``l2`` is excluded from every language-filtered browse once
    it is published (see public-courses.instructions.md).

    The v2 preparation requires a target language, so a claim always has one.
    ``l2`` is still omitted rather than written empty when it is blank,
    because the catalog treats an empty value as absent and a key that is
    sometimes null and sometimes missing is two shapes for one state.
    """
    content: Dict[str, Any] = {"uuid": course_plan_id}
    if isinstance(target_language, str) and target_language.strip():
        content["l2"] = target_language.strip()
    return content
