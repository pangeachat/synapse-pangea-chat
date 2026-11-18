#!/usr/bin/env python3
"""Move the course spaces on one homeserver, then the learners' progress records,
from old Can-Do ids to merged ones.

Step 8 of the Can-Do migration (pangeachat/.github#832), run on each
environment right after that environment's plans are migrated. Course spaces
first, in two runs:

1. The dry run is READ-ONLY. It walks every space on the homeserver (or only
   the ``--room-id`` ones), asks the endpoint what it would change in each, and
   writes the answers to a report: entries combined, notes trimmed, pins
   pooled, spaces refused for having no local instructor. Read the report.

       MATRIX_ADMIN_TOKEN=... python scripts/migrate_course_can_dos.py \\
           --homeserver https://matrix.staging.pangea.chat \\
           --map staging-can-do-map.json --statements staging-statements.json \\
           --report staging-report.json

2. The apply writes exactly the spaces the report says would change, each only
   while it still holds the events the dry run read. A space a teacher edited
   since is skipped and listed; run a new dry run for it.

       MATRIX_ADMIN_TOKEN=... python scripts/migrate_course_can_dos.py \\
           --homeserver https://matrix.staging.pangea.chat \\
           --apply staging-report.json --results staging-results.json

Then, right after, the learners' progress records (``--progress``), each sent
as its learner. The dry run writes counts per course; the apply merges every
record again from its current state and history, so it needs no expectations,
and a re-run changes nothing. Run it again a few days later for apps that
wrote old ids back, again right before the old Can-Dos are deleted, and once
more a few weeks after, with the same map.

       MATRIX_ADMIN_TOKEN=... python scripts/migrate_course_can_dos.py --progress \\
           --homeserver https://matrix.staging.pangea.chat \\
           --map staging-can-do-map.json --report staging-progress.json
       MATRIX_ADMIN_TOKEN=... python scripts/migrate_course_can_dos.py --progress \\
           --homeserver https://matrix.staging.pangea.chat \\
           --apply staging-progress.json --results staging-progress-results.json

With ``--valid-ids`` (one JSON list: every Can-Do row id on THIS environment,
merged ones included), the dry run also removes from course lists and pins
every Can-Do id that is no Can-Do there, and reports each by id with counts of
what the teacher had set on it; the apply writes the same. Without it, unknown
ids are left as they are.

The map file is one JSON object: old Can-Do row id -> merged row id, for THIS
environment, since row ids differ between environments. The statements file is
one JSON object: Can-Do row id -> its statement, for every old and merged
Can-Do in the map, leaving out the old Can-Dos on the quest step's no-note
list. An entry with no note gets its old Can-Dos' statements as one, and the
report counts them as notes added. A report records the
homeserver, the map and which operation made it, and an apply refuses a
report made on another homeserver or by the other operation. Reports name
rooms, courses and learners: keep them out of the repo. A trimmed note is
reported as lengths and counts; its text stays in the room's history, in the
event the dry run read.

The progress apply exits 1 when any record is left with old ids or a lost
completion (refused, on another server, not the learner's own, failed, or an
old key left in place), so a person decides what to do with it. Deactivated
learners' records are listed on their own and do not fail it: their old ids
stay in rooms no app or account can read or write.

``MATRIX_ADMIN_TOKEN`` is a server admin's access token, supplied out-of-band
(org ``access-controls.instructions.md``). It is never printed.

The rules each endpoint applies are in its module docstring
(synapse_pangea_chat/migrate_course_can_dos/); the run order is the runbook skill
(2-step-choreographer migrate-plans-to-merged-can-dos).
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
MIGRATE_PATH = "/_synapse/client/pangea/v1/migrate_course_can_dos"
PROGRESS_PATH = "/_synapse/client/pangea/v1/migrate_course_progress"
SPACE_ROOM_TYPE = "m.space"
PAGE_SIZE = 500
MAX_PAGES = 1000
PAUSE_SECONDS = 0.05
CHANGED_SINCE_DRY_RUN = "The space changed since the dry run"


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
    # silent-ok: an error status is returned with its body, and every caller records or raises on it
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


def dry_run(
    homeserver: str,
    token: str,
    can_do_map: Dict[str, str],
    room_ids: Optional[List[str]] = None,
    statements: Optional[Dict[str, str]] = None,
    valid_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The report: the endpoint's dry-run answer for each space."""
    statements = statements or {}
    spaces = []
    for room_id in room_ids or walk_spaces(homeserver, token):
        request: Dict[str, Any] = {
            "room_id": room_id,
            "can_do_map": can_do_map,
            "statements": statements,
            "dry_run": True,
        }
        if valid_ids is not None:
            request["valid_ids"] = valid_ids
        status, body = _call(f"{homeserver}{MIGRATE_PATH}", token, request)
        spaces.append({"room_id": room_id, "status": status, "body": body})
        time.sleep(PAUSE_SECONDS)
    return {
        "kind": "spaces",
        "homeserver": homeserver,
        "can_do_map": can_do_map,
        "statements": statements,
        "valid_ids": valid_ids,
        "spaces": spaces,
    }


def to_write(space: Dict[str, Any]) -> bool:
    return space["status"] == 200 and bool(space["body"].get("writes"))


def _check_report(report: Dict[str, Any], homeserver: str, kind: str) -> None:
    if report["homeserver"] != homeserver:
        raise SystemExit(
            f"The report was made on {report['homeserver']}, not {homeserver}"
        )
    if report.get("kind") != kind:
        raise SystemExit(f"The report is not a {kind} report")


def apply(homeserver: str, token: str, report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Write each space the report says would change, as its dry run saw it."""
    _check_report(report, homeserver, "spaces")
    results = []
    for space in filter(to_write, report["spaces"]):
        request: Dict[str, Any] = {
            "room_id": space["room_id"],
            "can_do_map": report["can_do_map"],
            "statements": report.get("statements") or {},
            "expected": space["body"]["expected"],
        }
        if report.get("valid_ids") is not None:
            request["valid_ids"] = report["valid_ids"]
        status, body = _call(f"{homeserver}{MIGRATE_PATH}", token, request)
        results.append({"room_id": space["room_id"], "status": status, "body": body})
        time.sleep(PAUSE_SECONDS)
    return results


def summarize_report(report: Dict[str, Any]) -> List[str]:
    spaces = report["spaces"]
    copies = [s["body"].get("course_missions") or {} for s in spaces]
    pins = [s["body"].get("teacher_mode") or {} for s in spaces]
    lines = [
        f"{len(spaces)} spaces checked",
        f"{sum(map(to_write, spaces))} to rewrite",
        f"{sum(len(c.get('combined', [])) for c in copies)} entries combined, "
        f"{sum(len(e['slots_given_up']) for c in copies for e in c.get('combined', []))}"
        " slots given up",
        f"{sum(len(c.get('notes_added', [])) for c in copies)} entries gain a note "
        "from the old wording",
        f"{sum(len(c.get('notes_trimmed', [])) for c in copies)} notes trimmed",
        "combining leaves out "
        f"{sum(e.get('due_dates_dropped', 0) for c in copies for e in c.get('combined', []))}"
        " due dates, "
        f"{sum(e.get('cities_dropped', 0) for c in copies for e in c.get('combined', []))}"
        " cities, "
        f"{sum(len(e.get('fields_dropped', [])) for c in copies for e in c.get('combined', []))}"
        " other fields, and shows "
        f"{sum(e.get('hidden_now_shown', 0) for c in copies for e in c.get('combined', []))}"
        " hidden activities",
        f"{sum(len(c.get('removed_combined', [])) for c in copies)} removed Can-Dos "
        f"combined, {sum(len(c.get('removed_into_missions', [])) for c in copies)} "
        "joined a shown Can-Do",
        f"{sum(len(p.get('pooled', [])) for p in pins)} pin sets pooled, "
        f"{sum(len(p.get('unpinned_siblings', [])) for p in pins)} with an unpinned "
        f"old Can-Do, {sum(p.get('pin_values_dropped', 0) for p in pins)} pin "
        "values dropped",
    ]
    unknown = [e for c in copies for e in c.get("unknown_removed", [])]
    unknown_pins = sum(len(p.get("unknown_removed", [])) for p in pins)
    if unknown or unknown_pins:
        lines.append(
            f"{len(unknown)} entries naming no Can-Do removed from lists "
            f"({sum(e['activities'] for e in unknown)} activities, "
            f"{sum(e['note'] for e in unknown)} notes, "
            f"{sum(e['due_at'] for e in unknown)} due dates), "
            f"{unknown_pins} from pins: "
            + " ".join(sorted({e["lo_id"] for e in unknown}))
        )
    for label, matches in (
        ("refused, no local instructor", lambda s: s["status"] == 409),
        (
            "Can-Do list left alone, readers ignore it",
            lambda s: (s["body"].get("course_missions") or {}).get("outcome")
            in ("ignored_by_readers", "rewrite_failed"),
        ),
        (
            "left with old ids",
            lambda s: bool(
                (s["body"].get("course_missions") or {}).get("old_ids_left")
            ),
        ),
        (
            "pins unreadable, left alone",
            lambda s: (s["body"].get("teacher_mode") or {}).get("outcome")
            == "unreadable",
        ),
        ("failed", lambda s: s["status"] not in (200, 409)),
    ):
        rooms = [s["room_id"] for s in spaces if matches(s)]
        if rooms:
            lines.append(f"{len(rooms)} {label}: {' '.join(rooms)}")
    missing = sorted({i for c in copies for i in c.get("no_statement", [])})
    if missing:
        lines.append(
            f"{len(missing)} old Can-Dos listed with no statement, so no wording: "
            + " ".join(missing)
        )
    return lines


def summarize_results(results: List[Dict[str, Any]]) -> List[str]:
    lines = [
        f"{sum(r['status'] == 200 and r['body']['changed'] for r in results)} written"
    ]
    for label, matches in (
        (
            "already had nothing left to change",
            lambda r: r["status"] == 200 and not r["body"]["changed"],
        ),
        (
            "skipped, changed since the dry run",
            lambda r: r["body"].get("error") == CHANGED_SINCE_DRY_RUN,
        ),
        (
            "refused",
            lambda r: r["status"] == 409
            and r["body"].get("error") != CHANGED_SINCE_DRY_RUN,
        ),
        ("failed", lambda r: r["status"] not in (200, 409)),
    ):
        rooms = [r["room_id"] for r in results if matches(r)]
        if rooms:
            lines.append(f"{len(rooms)} {label}: {' '.join(rooms)}")
    return lines


def run_progress(
    homeserver: str, token: str, can_do_map: Dict[str, str], dry_run: bool
) -> List[Dict[str, Any]]:
    """Every progress record, page by page; a failed page stops the run."""
    records: List[Dict[str, Any]] = []
    after = None
    while True:
        body: Dict[str, Any] = {"can_do_map": can_do_map, "dry_run": dry_run}
        if after is not None:
            body["after"] = after
        status, page = _call(f"{homeserver}{PROGRESS_PATH}", token, body)
        if status != 200:
            raise RuntimeError(f"A page failed after {after}: HTTP {status}: {page}")
        records.extend(page["records"])
        after = page["next"]
        if after is None:
            return records
        time.sleep(PAUSE_SECONDS)


UNRESOLVED_OUTCOMES = (
    "refused",
    "not_learners_own",
    "not_local",
    "failed",
)


def unresolved(record: Dict[str, Any]) -> bool:
    """Left with old ids or a lost completion; a deactivated learner's record
    is counted on its own instead."""
    if record["outcome"] == "deactivated":
        return False
    return record["outcome"] in UNRESOLVED_OUTCOMES or bool(record.get("old_keys_left"))


def per_course(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Counts per course: records, rewrites, keys moved, completions kept and
    restored."""
    counts: Dict[str, Dict[str, int]] = {}
    for record in records:
        course = counts.setdefault(
            record["course_id"],
            {
                "records": 0,
                "rewrite": 0,
                "keys_moved": 0,
                "completions_kept": 0,
                "completions_restored": 0,
            },
        )
        course["records"] += 1
        course["rewrite"] += record["outcome"] == "rewrite"
        course["keys_moved"] += record["keys_moved"]
        course["completions_kept"] += record["completions_kept"]
        course["completions_restored"] += record.get("completions_restored", 0)
    return counts


def summarize_progress(records: List[Dict[str, Any]], dry_run: bool) -> List[str]:
    courses = per_course(records)
    lines = [
        f"{len(records)} records in {len(courses)} courses",
        f"{sum(r['outcome'] == 'rewrite' for r in records)} "
        + ("to rewrite" if dry_run else "rewritten")
        + f", in {sum(1 for c in courses.values() if c['rewrite'])} courses",
        f"{sum(r['keys_moved'] for r in records)} keys moved, "
        f"{sum(r['completions_kept'] for r in records)} completions kept, "
        f"{sum(r.get('completions_restored', 0) for r in records)} restored",
        f"{sum(r.get('dates_unreadable', 0) for r in records)} unreadable dates "
        "passed over",
    ]
    left = [
        f"{r['user_id']} {r['room_id']} {r['course_id']}"
        for r in records
        if r["outcome"] == "unchanged" and r.get("old_keys_left")
    ]
    if left:
        lines.append(f"{len(left)} with an old key left in place:")
        lines.extend(f"  {line}" for line in left)
    deactivated = [
        f"{r['user_id']} {r['room_id']} {r['course_id']}"
        for r in records
        if r["outcome"] == "deactivated"
    ]
    if deactivated:
        lines.append(f"{len(deactivated)} deactivated (outside the limit):")
        lines.extend(f"  {line}" for line in deactivated)
    for outcome in ("unreadable", *UNRESOLVED_OUTCOMES):
        ids = [
            f"{r['user_id']} {r['room_id']} {r['course_id']}"
            for r in records
            if r["outcome"] == outcome
        ]
        if ids:
            lines.append(f"{len(ids)} {outcome}:")
            lines.extend(f"  {line}" for line in ids)
    return lines


def _write_json(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=1, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--homeserver", required=True)
    parser.add_argument("--map", help="Dry run: the old -> merged Can-Do id map.")
    parser.add_argument(
        "--statements",
        help="Dry run: Can-Do row id -> statement, for the old Can-Dos' notes.",
    )
    parser.add_argument("--report", help="Dry run: where to write the report.")
    parser.add_argument(
        "--valid-ids",
        help="Dry run: every Can-Do row id on this environment, to remove the rest.",
    )
    parser.add_argument(
        "--room-id",
        action="append",
        default=[],
        help="Dry run: a space to check; repeatable. Without it, every space.",
    )
    parser.add_argument("--apply", help="A dry run's report, to write.")
    parser.add_argument("--results", help="Apply: where to write the results.")
    parser.add_argument(
        "--progress",
        action="store_true",
        help="The learners' progress records instead of the course spaces.",
    )
    args = parser.parse_args()
    homeserver = args.homeserver.rstrip("/")
    token = os.environ.get("MATRIX_ADMIN_TOKEN")
    if not token:
        print("MATRIX_ADMIN_TOKEN is not set", file=sys.stderr)
        return 2

    if args.progress:
        return _progress(args, parser, homeserver, token)
    if args.apply:
        if not args.results:
            parser.error("--apply needs --results")
        with open(args.apply, encoding="utf-8") as f:
            report = json.load(f)
        results = apply(homeserver, token, report)
        _write_json(args.results, results)
        print("\n".join(summarize_results(results)))
        return 0 if all(r["status"] == 200 for r in results) else 1

    if not args.map or not args.statements or not args.report:
        parser.error("a dry run needs --map, --statements and --report")
    with open(args.map, encoding="utf-8") as f:
        can_do_map = json.load(f)
    with open(args.statements, encoding="utf-8") as f:
        statements = json.load(f)
    valid_ids = None
    if args.valid_ids:
        with open(args.valid_ids, encoding="utf-8") as f:
            valid_ids = json.load(f)
    report = dry_run(homeserver, token, can_do_map, args.room_id, statements, valid_ids)
    _write_json(args.report, report)
    print("\n".join(summarize_report(report)))
    return 0


def _progress(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    homeserver: str,
    token: str,
) -> int:
    if args.apply:
        if not args.results:
            parser.error("--apply needs --results")
        with open(args.apply, encoding="utf-8") as f:
            report = json.load(f)
        _check_report(report, homeserver, "progress")
        records = run_progress(homeserver, token, report["can_do_map"], False)
        _write_json(
            args.results, {"records": records, "per_course": per_course(records)}
        )
        print("\n".join(summarize_progress(records, dry_run=False)))
        return 1 if any(map(unresolved, records)) else 0
    if not args.map or not args.report:
        parser.error("a dry run needs --map and --report")
    with open(args.map, encoding="utf-8") as f:
        can_do_map = json.load(f)
    records = run_progress(homeserver, token, can_do_map, True)
    _write_json(
        args.report,
        {
            "kind": "progress",
            "homeserver": homeserver,
            "can_do_map": can_do_map,
            "records": records,
            "per_course": per_course(records),
        },
    )
    print("\n".join(summarize_progress(records, dry_run=True)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
