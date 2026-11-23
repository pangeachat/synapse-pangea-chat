"""Test setup for requested courses, shared by the E2E tests that need one.

A requested course is made the way production makes it: the v2 preparation
emails the claim link, and the teacher claims it, which creates the space
(create-course-space.instructions.md). Courses made before that path existed
are room-backed claims the bot created; ``legacy_course`` builds one directly,
because no endpoint makes them any more and the claim and reminder paths for
them must keep working (knock-with-code.instructions.md, "Claiming a course").
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict
from urllib.parse import quote

import psycopg2
import requests

from synapse_pangea_chat.email_invite.course_claims import (
    _CREATE_CODE_TABLE_SQL,
    _CREATE_TABLE_SQL,
    admin_code_digest,
)
from synapse_pangea_chat.email_invite.course_space_defaults import (
    DEFAULT_SPACE_POWER_LEVELS,
    PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
)
from tests.smtp_sink import SmtpSink, body_text

PREPARE_PATH = "/_synapse/client/pangea/v2/create_course_space"
KNOCK_WITH_CODE_PATH = "/_synapse/client/pangea/v1/knock_with_code"


def _post(server_url: str, path: str, token: str, body: Dict[str, Any]) -> Any:
    response = requests.post(
        server_url + path,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if response.status_code != 200:
        raise AssertionError(f"{path} answered {response.status_code}: {response.text}")
    return response.json()


def claimed_course(
    server_url: str,
    sink: SmtpSink,
    app_base_url: str,
    operator: str,
    teacher: str,
    teacher_email: str,
    request_key: str,
    **spec: Any,
) -> str:
    """Prepare an invitation as ``operator``, take the claim link from the
    email it sends to ``teacher_email`` and claim it as ``teacher``. Returns
    the course room, which the claim created with ``teacher`` as its admin.
    ``spec`` overrides the title, quest and language."""
    body = {
        "request_key": request_key,
        "teacher_email": teacher_email,
        "title": "Spanish 1",
        "course_plan_id": "plan-requested",
        "target_language": "es",
        **spec,
    }
    _post(server_url, PREPARE_PATH, operator, body)
    ready = sink.wait_for(teacher_email, "Your quest is ready")
    if ready is None:
        raise AssertionError("the preparation sent no claim email")
    found = re.search(re.escape(app_base_url) + r"/([a-zA-Z0-9]{7})", body_text(ready))
    if found is None:
        raise AssertionError("the claim email carries no claim link")
    answer = _post(
        server_url, KNOCK_WITH_CODE_PATH, teacher, {"access_code": found.group(1)}
    )
    rooms = answer.get("already_joined") or []
    if len(rooms) != 1:
        raise AssertionError(f"the claim answered no course: {answer}")
    return rooms[0]


def legacy_course(
    server_url: str,
    database_url: str,
    bot: str,
    bot_user_id: str,
    requested_email: str | None,
    admin_code: str,
    class_code: str,
    title: str = "Spanish 1",
    course_plan_id: str = "plan-legacy",
) -> str:
    """A room-backed requested course as the retired v1 endpoint left one: a
    space the bot created and administers, with the class code in its join
    rules and the claim record, holding the requesting address and the admin
    code's fingerprint, out of room state. Returns the room."""
    power = {**DEFAULT_SPACE_POWER_LEVELS, "users": {bot_user_id: 100}}
    created = _post(
        server_url,
        "/_matrix/client/v3/createRoom",
        bot,
        {
            "preset": "private_chat",
            "name": title,
            "visibility": "private",
            "creation_content": {"type": "m.space"},
            "power_level_content_override": power,
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {"join_rule": "knock", "access_code": class_code},
                },
                {
                    "type": PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
                    "state_key": "",
                    "content": {"uuid": course_plan_id, "l2": "es"},
                },
            ],
        },
    )
    room_id = created["room_id"]
    digest = admin_code_digest(admin_code)
    now = int(time.time() * 1000)
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        # The store makes its tables on first use; this may be the first.
        cur.execute(_CREATE_TABLE_SQL)
        cur.execute(_CREATE_CODE_TABLE_SQL)
        cur.execute(
            "INSERT INTO pangea_course_claim"
            " (room_id, requested_email, admin_code_sha256, created_at_ms)"
            " VALUES (%s, %s, %s, %s)",
            (room_id, requested_email, digest, now),
        )
        cur.execute(
            "INSERT INTO pangea_course_claim_code"
            " (admin_code_sha256, room_id, created_at_ms) VALUES (%s, %s, %s)",
            (digest, room_id, now),
        )
    return room_id


def room_url(server_url: str, room_id: str, suffix: str) -> str:
    return f"{server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/{suffix}"
