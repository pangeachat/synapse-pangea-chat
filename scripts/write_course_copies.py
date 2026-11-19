#!/usr/bin/env python3
"""Give each course space with no Can-Do list of its own one, built from its quest.

Courses got their own Can-Do list (``pangea.course_missions``) at creation from
mid-October 2026; a space made before that, or one whose creation read failed,
has none, and the dashboard refuses such a course (admin-dash-api#472). For
each one, this sends set_course_plan with the space's own quest as both the
current and the new quest and its current name and topic, so the endpoint only
builds the list (set_course_plan.py module docstring, step 3). A space that holds
a list, empty or not, is left alone.

    MATRIX_ADMIN_TOKEN=... python scripts/write_course_copies.py \\
        --homeserver https://matrix.staging.pangea.chat            # dry run
    MATRIX_ADMIN_TOKEN=... python scripts/write_course_copies.py \\
        --homeserver https://matrix.staging.pangea.chat --commit

With ``--rooms FILE`` it instead replaces the lists named in the file, one
per line as ``<room id> <list event id>`` (``#`` starts a comment): the lists
the builder made from the newest 200 library rows only, before it read a
quest's selected activities by id (pangeachat/.github#832). Each goes through
set_course_plan's ``replace_copy_event_id``, so a room whose list is no longer
that event (a teacher published since) is refused and left alone. The dry run
prints, per room, the activities in the current list, in the rebuilt one, and
the current ones outside the quest's selection.

    MATRIX_ADMIN_TOKEN=... python scripts/write_course_copies.py \\
        --homeserver https://matrix.staging.pangea.chat --rooms rebuild.txt

``MATRIX_ADMIN_TOKEN`` is a server admin's access token, supplied out-of-band
(org ``access-controls.instructions.md``). It is never printed. The run prints
room and quest ids only, exits 1 when any space needs a look (unreadable,
refused, or no list could be built), and is safe to send again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

ADMIN_ROOMS_PATH = "/_synapse/admin/v1/rooms"
SET_COURSE_PLAN_PATH = "/_synapse/client/pangea/v1/set_course_plan"
COURSE_PLAN_TYPES = ("pangea.course_plan",)
PLAN_ID_KEYS = ("uuid", "course_plan_id")
COURSE_MISSIONS_TYPE = "pangea.course_missions"
SPACE_ROOM_TYPE = "m.space"
PAGE_SIZE = 500
MAX_PAGES = 1000
PAUSE_SECONDS = 0.05
PRODUCTION_HOST = "matrix.pangea.chat"


def _call(
    url: str, token: str, body: Optional[Dict[str, Any]] = None
) -> Tuple[int, Any]:
    """The HTTP status and JSON answer; an error status is an answer too."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if body else "GET")
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status, raw = response.status, response.read()
    # silent-ok: an error status is returned with its body, and every caller reports it
    except urllib.error.HTTPError as e:
        with e:
            status, raw = e.code, e.read()
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    # silent-ok: a body that is not JSON is returned as the error text, with its status
    except ValueError:
        return status, {"error": raw.decode("utf-8", "replace")[:300]}


def walk_spaces(homeserver: str, token: str) -> Iterator[str]:
    """Every space on the homeserver that still has a local member."""
    start = 0
    for _ in range(MAX_PAGES):
        query = urllib.parse.urlencode({"from": start, "limit": PAGE_SIZE})
        status, page = _call(f"{homeserver}{ADMIN_ROOMS_PATH}?{query}", token)
        if status != 200:
            raise RuntimeError(f"Listing rooms failed: HTTP {status}: {page}")
        for room in page.get("rooms", []):
            if (
                room.get("room_type") == SPACE_ROOM_TYPE
                and room.get("joined_local_members", 1) > 0
            ):
                yield room["room_id"]
        if "next_batch" not in page:
            return
        start = page["next_batch"]
    raise RuntimeError(f"More than {MAX_PAGES} pages of rooms; refusing to continue")


def _state(events: List[Dict[str, Any]], event_type: str) -> Optional[Dict[str, Any]]:
    for event in events:
        content = event.get("content")
        if (
            event.get("type") == event_type
            and event.get("state_key", "") == ""
            and isinstance(content, dict)
        ):
            return content
    return None


def _plan_request(
    events: List[Dict[str, Any]], room_id: str
) -> Optional[Dict[str, Any]]:
    """The set_course_plan body that keeps this space on its quest, name and
    topic, or None when it is not a course."""
    plan = _state(events, COURSE_PLAN_TYPES[0]) or {}
    quest = next(
        (plan[k] for k in PLAN_ID_KEYS if isinstance(plan.get(k), str) and plan[k]),
        None,
    )
    if quest is None:
        return None
    name = (_state(events, "m.room.name") or {}).get("name")
    topic = (_state(events, "m.room.topic") or {}).get("topic")
    return {
        "room_id": room_id,
        "quest_id": quest,
        "expected_quest_id": quest,
        "title": name if isinstance(name, str) else "",
        "description": topic if isinstance(topic, str) else "",
    }


def parse_rooms(text: str) -> List[Tuple[str, str]]:
    """``(room id, list event id)`` per line of a --rooms file."""
    rooms: List[Tuple[str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if (
            len(parts) != 2
            or not parts[0].startswith("!")
            or not parts[1].startswith("$")
        ):
            raise SystemExit(
                f"--rooms line {number}: expected '<room id> <list event id>'"
            )
        rooms.append((parts[0], parts[1]))
    return rooms


def _room_events(
    homeserver: str, token: str, room_id: str
) -> Optional[List[Dict[str, Any]]]:
    path = f"{ADMIN_ROOMS_PATH}/{urllib.parse.quote(room_id, safe='')}/state"
    status, page = _call(f"{homeserver}{path}", token)
    if status != 200:
        print(f"  {room_id}: state unreadable (HTTP {status})")
        return None
    return page.get("state") or []


def run_replace(
    homeserver: str, token: str, rooms: List[Tuple[str, str]], commit: bool
) -> int:
    """Replace each named list; per room, what changes."""
    replaced, problems = 0, 0
    for room_id, event_id in rooms:
        events = _room_events(homeserver, token, room_id)
        body = _plan_request(events, room_id) if events is not None else None
        if body is None:
            if events is not None:
                print(f"  {room_id}: not a course")
            problems += 1
            continue
        if not body["title"]:
            print(f"  {room_id}: no name; set_course_plan needs one")
            problems += 1
            continue
        status, answer = _call(
            f"{homeserver}{SET_COURSE_PLAN_PATH}",
            token,
            {**body, "replace_copy_event_id": event_id, "dry_run": not commit},
        )
        answer = answer if isinstance(answer, dict) else {}
        built = answer.get("can_do_list") or {}
        if status == 409 and "current_copy_event_id" in answer:
            print(
                f"  {room_id}: left alone, its list was published over "
                f"(now {answer['current_copy_event_id']})"
            )
            problems += 1
        elif status != 200 or not built.get("built"):
            print(
                f"  {room_id}: quest {body['quest_id']} HTTP {status} "
                f"{answer.get('error') or ''} {built}"
            )
            problems += 1
        else:
            print(
                f"  {room_id}: quest {body['quest_id']}: "
                f"{built.get('current_activities')} activities now, "
                f"{built.get('rebuilt_activities')} rebuilt, "
                f"{built.get('current_outside_selection')} outside the selection"
            )
            replaced += 1
        time.sleep(PAUSE_SECONDS)
    print(
        f"\n{replaced} {'replaced' if commit else 'would be replaced'}, "
        f"{problems} need a look"
    )
    return 1 if problems else 0


def needed_request(
    events: List[Dict[str, Any]], room_id: str
) -> Optional[Dict[str, Any]]:
    """The set_course_plan body that builds this space's list, or None when it
    is not a course or already holds a list."""
    if _state(events, COURSE_MISSIONS_TYPE) is not None:
        return None
    return _plan_request(events, room_id)


def run(homeserver: str, token: str, commit: bool) -> int:
    written, problems, skipped = 0, 0, 0
    for room_id in walk_spaces(homeserver, token):
        path = f"{ADMIN_ROOMS_PATH}/{urllib.parse.quote(room_id, safe='')}/state"
        status, page = _call(f"{homeserver}{path}", token)
        if status != 200:
            print(f"  {room_id}: state unreadable (HTTP {status})")
            problems += 1
            continue
        body = needed_request(page.get("state") or [], room_id)
        if body is None:
            skipped += 1
            continue
        if not body["title"]:
            print(f"  {room_id}: no name; set_course_plan needs one")
            problems += 1
            continue
        status, answer = _call(
            f"{homeserver}{SET_COURSE_PLAN_PATH}",
            token,
            {**body, "dry_run": not commit},
        )
        built = (answer.get("can_do_list") or {}) if isinstance(answer, dict) else {}
        if status != 200 or not built.get("built"):
            error = answer.get("error") if isinstance(answer, dict) else None
            print(
                f"  {room_id}: quest {body['quest_id']} HTTP {status} {error or ''} {built}"
            )
            problems += 1
        else:
            print(
                f"  {room_id}: quest {body['quest_id']} -> {built.get('missions')} Can-Dos"
            )
            written += 1
        time.sleep(PAUSE_SECONDS)
    print(
        f"\n{written} {'written' if commit else 'would be written'}, "
        f"{skipped} left alone (have a list or are not courses), {problems} need a look"
    )
    return 1 if problems else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--homeserver", required=True)
    parser.add_argument(
        "--commit", action="store_true", help="write (default: dry run)"
    )
    parser.add_argument("--confirm-production", action="store_true")
    parser.add_argument(
        "--rooms",
        help="replace the lists named in this file (<room id> <list event id> "
        "per line) instead of building missing ones",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("MATRIX_ADMIN_TOKEN")
    if not token:
        raise SystemExit("MATRIX_ADMIN_TOKEN must be set")
    homeserver = args.homeserver.rstrip("/")
    if (
        urllib.parse.urlparse(homeserver).hostname == PRODUCTION_HOST
        and not args.confirm_production
    ):
        raise SystemExit(
            f"REFUSING: {homeserver} is production; pass --confirm-production"
        )
    print("COMMIT" if args.commit else "DRY RUN")
    if args.rooms:
        with open(args.rooms, encoding="utf-8") as f:
            rooms = parse_rooms(f.read())
        return run_replace(homeserver, token, rooms, args.commit)
    return run(homeserver, token, args.commit)


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
