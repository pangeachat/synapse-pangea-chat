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

A list Content Studio published (a field the builder never writes: a time
zone, a due date, removals) is refused and reported, unless the room is named
with ``--allow-teacher-list <room id>``.

``--keep-progress`` follows a quest whose choices were trimmed without
costing anyone: each Can-Do shows the quest's chosen activities, every id the
list named that is no longer shown moves to ``removed_activity_ids`` (hidden,
still credited), and due dates, notes, the time zone and the course's own
Can-Dos stay (set_course_plan/merge_can_do_list.py). The dry run prints, per
room and Can-Do, shown before and after, moved to removed and what is lost;
"nothing lost" is the server's comparison of the two lists, and a list that
would lose anything is refused, not written. A commit prints, and with
``--undo-out FILE`` appends, one undo line per room written:
``<room id> <new list event id> <replaced list event id>``.

    MATRIX_ADMIN_TOKEN=... python scripts/write_course_copies.py \\
        --homeserver https://matrix.staging.pangea.chat --rooms trimmed.txt \\
        --keep-progress [--commit --undo-out undo.txt]

``--undo FILE`` writes each room's earlier list back as a new revision, only
while the room still holds the list its line names:

    MATRIX_ADMIN_TOKEN=... python scripts/write_course_copies.py \\
        --homeserver https://matrix.staging.pangea.chat --undo undo.txt [--commit]

Production needs ``--confirm-production`` on every run, dry runs included.

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
from typing import IO, AbstractSet, Any, Dict, Iterator, List, Optional, Tuple

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


def parse_undo(text: str) -> List[Tuple[str, str, str]]:
    """``(room id, current list event id, earlier list event id)`` per line of
    an --undo file, as a --keep-progress commit writes them."""
    rooms: List[Tuple[str, str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if (
            len(parts) != 3
            or not parts[0].startswith("!")
            or not all(p.startswith("$") for p in parts[1:])
        ):
            raise SystemExit(
                f"--undo line {number}: expected "
                "'<room id> <current list event id> <earlier list event id>'"
            )
        rooms.append((parts[0], parts[1], parts[2]))
    return rooms


def _list_event_id(events: List[Dict[str, Any]]) -> Optional[str]:
    for event in events:
        if (
            event.get("type") == COURSE_MISSIONS_TYPE
            and event.get("state_key", "") == ""
        ):
            event_id = event.get("event_id")
            return event_id if isinstance(event_id, str) else None
    return None


def _print_merge(room_id: str, quest_id: str, built: Dict[str, Any]) -> None:
    """Per room and Can-Do, what following the quest changes; "nothing lost"
    only when the server found nothing lost."""
    lost = built.get("lost") or []
    verdict = "nothing lost" if built.get("nothing_lost") and not lost else "LOSES:"
    print(f"  {room_id}: quest {quest_id}: {built.get('missions')} Can-Dos, {verdict}")
    for item in lost:
        print(f"      lost {item}")
    for entry in built.get("entries") or []:
        if entry.get("status") == "removed_by_course":
            print(
                f"    {entry['lo_id']}: removed_by_course, left out as the course has it"
            )
            continue
        line = (
            f"    {entry['lo_id']}: {entry['status']}, "
            f"shown {entry['shown_before']} -> {entry['shown_after']}"
        )
        if entry["status"] != "course_only":
            line += (
                f", {entry['moved_to_removed']} moved to removed, "
                f"{entry['back_to_shown']} shown again, "
                f"{entry['removed_after']} removed in all"
            )
            if entry.get("note"):
                line += f", note {entry['note']}"
        if entry.get("shows_none"):
            line += ", SHOWS NONE (the app hides this Can-Do; its credit stays)"
        print(line)


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
    homeserver: str,
    token: str,
    rooms: List[Tuple[str, str]],
    commit: bool,
    keep_progress: bool = False,
    allow_teacher_list: AbstractSet[str] = frozenset(),
    undo_out: Optional[IO[str]] = None,
) -> int:
    """Replace each named list; per room, what changes. With
    ``keep_progress`` the list follows its quest and keeps every id it credits
    (set_course_plan ``keep_progress``), and each room written leaves an undo
    line: ``<room id> <new list event id> <replaced list event id>``."""
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
        request = {**body, "replace_copy_event_id": event_id}
        if keep_progress:
            request["keep_progress"] = True
        if room_id in allow_teacher_list:
            request["allow_teacher_list"] = True
        status, answer = _call(
            f"{homeserver}{SET_COURSE_PLAN_PATH}",
            token,
            {**request, "dry_run": not commit},
        )
        answer = answer if isinstance(answer, dict) else {}
        built = answer.get("can_do_list") or {}
        if status == 409 and answer.get("code") == "teacher_list":
            print(
                f"  {room_id}: refused, a teacher published this list "
                "(name the room with --allow-teacher-list to follow the quest anyway)"
            )
            problems += 1
        elif status == 409 and answer.get("code") == "would_lose":
            print(f"  {room_id}: refused, following the quest would lose:")
            for item in answer.get("lost") or []:
                print(f"      {item}")
            problems += 1
        elif status == 409 and "current_copy_event_id" in answer:
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
            if keep_progress:
                _print_merge(room_id, body["quest_id"], built)
            replaced += 1
            if commit and keep_progress:
                after = _room_events(homeserver, token, room_id)
                new_id = _list_event_id(after) if after is not None else None
                if new_id is None:
                    print(
                        f"    undo: {room_id}: new list event unreadable; find it in the room"
                    )
                    problems += 1
                else:
                    line = f"{room_id} {new_id} {event_id}"
                    print(f"    undo: {line}")
                    if undo_out is not None:
                        undo_out.write(line + "\n")
                        undo_out.flush()
        time.sleep(PAUSE_SECONDS)
    print(
        f"\n{replaced} {'replaced' if commit else 'would be replaced'}, "
        f"{problems} need a look"
    )
    return 1 if problems else 0


def run_undo(
    homeserver: str, token: str, rooms: List[Tuple[str, str, str]], commit: bool
) -> int:
    """Write each room's earlier list back over the named current one."""
    restored, problems = 0, 0
    for room_id, current_id, earlier_id in rooms:
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
            {
                **body,
                "replace_copy_event_id": current_id,
                "restore_copy_event_id": earlier_id,
                "dry_run": not commit,
            },
        )
        answer = answer if isinstance(answer, dict) else {}
        report = answer.get("can_do_list") or {}
        if status == 409 and "current_copy_event_id" in answer:
            print(
                f"  {room_id}: left alone, its list was published over "
                f"(now {answer['current_copy_event_id']})"
            )
            problems += 1
        elif status != 200:
            print(f"  {room_id}: HTTP {status} {answer.get('error') or ''}")
            problems += 1
        else:
            print(
                f"  {room_id}: {earlier_id} restored over {current_id}, "
                f"revision {report.get('previous_revision')} -> {report.get('revision')}"
            )
            restored += 1
        time.sleep(PAUSE_SECONDS)
    print(
        f"\n{restored} {'restored' if commit else 'would be restored'}, "
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
    parser.add_argument(
        "--keep-progress",
        action="store_true",
        help="with --rooms: follow the quest, moving every id no longer chosen "
        "to removed_activity_ids and keeping due dates, notes and the course's "
        "own Can-Dos",
    )
    parser.add_argument(
        "--allow-teacher-list",
        action="append",
        default=[],
        metavar="ROOM_ID",
        help="with --rooms: replace this room's list even though a teacher "
        "published it (repeat per room)",
    )
    parser.add_argument(
        "--undo-out",
        help="with --rooms --keep-progress --commit: append each written room's "
        "undo line to this file",
    )
    parser.add_argument(
        "--undo",
        help="write each room's earlier list back (<room id> <current list "
        "event id> <earlier list event id> per line, as --undo-out writes them)",
    )
    args = parser.parse_args(argv)
    if (
        args.keep_progress or args.allow_teacher_list or args.undo_out
    ) and not args.rooms:
        raise SystemExit(
            "--keep-progress, --allow-teacher-list and --undo-out need --rooms"
        )
    if args.undo and args.rooms:
        raise SystemExit("--undo and --rooms are separate runs")
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
        if not args.undo_out:
            return run_replace(
                homeserver,
                token,
                rooms,
                args.commit,
                keep_progress=args.keep_progress,
                allow_teacher_list=frozenset(args.allow_teacher_list),
            )
        with open(args.undo_out, "a", encoding="utf-8") as undo_out:
            return run_replace(
                homeserver,
                token,
                rooms,
                args.commit,
                keep_progress=args.keep_progress,
                allow_teacher_list=frozenset(args.allow_teacher_list),
                undo_out=undo_out,
            )
    if args.undo:
        with open(args.undo, encoding="utf-8") as f:
            return run_undo(homeserver, token, parse_undo(f.read()), args.commit)
    return run(homeserver, token, args.commit)


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
