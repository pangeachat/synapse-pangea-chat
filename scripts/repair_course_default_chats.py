#!/usr/bin/env python3
"""List the spaces whose default chats are out of line, and repair them one by one.

The course default chats module keeps a space's Announcements and
Introductions chats complete from the moment it runs; spaces that existed
before it need one repair. The repair endpoint deliberately takes ONE space
per call, so the decision of which spaces to touch is made here, in the open,
and not by an endpoint that could sweep the whole homeserver.

Without ``--apply`` the script is READ-ONLY: it walks every space on the
homeserver (or only the ``--room-id`` ones given), asks the endpoint for a dry
run of each, and prints the ones where something would change. With
``--apply`` it then repairs exactly those, one call per space: the endpoint
answers at once and works in the background, and the script re-checks each
space with dry runs until nothing is left to change, then prints the outcome.
Run it without ``--apply`` first and read the list.

Usage:
    MATRIX_ADMIN_TOKEN=... python scripts/repair_course_default_chats.py \
        --homeserver https://matrix.staging.pangea.chat [--apply] [--room-id !x:server ...]

``MATRIX_ADMIN_TOKEN`` is a server admin's access token, supplied out-of-band
(the org ``access-controls.instructions.md`` says how one is minted and
handled): both the room walk (the Synapse Admin API) and the repair endpoint
are admin-only. It is never printed.

Design: .github/instructions/course-default-chats.instructions.md
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
from typing import Any, Dict, Iterator, List, Optional

ADMIN_ROOMS_PATH = "/_synapse/admin/v1/rooms"
REPAIR_PATH = "/_synapse/client/pangea/v1/sync_course_default_chats"
SPACE_ROOM_TYPE = "m.space"
PAGE_SIZE = 100
PAGE_PAUSE_SECONDS = 0.2
MAX_PAGES = 1000
POLL_SECONDS = 3.0
# Joins are paced by Synapse's per-room join limit, about one a second after
# a burst of ten, so a large class takes minutes.
DEFAULT_WAIT_SECONDS = 900


class RequestFailed(Exception):
    """One HTTP request to the homeserver failed; the message says how."""


def _request_json(
    url: str, token: str, body: Optional[Dict[str, Any]] = None, timeout: int = 120
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if body else "GET")
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RequestFailed(
            f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
        ) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RequestFailed(f"{type(e).__name__}: {e}") from e
    try:
        return json.loads(raw)
    except ValueError as e:
        raise RequestFailed(f"non-JSON response: {raw[:300]}") from e


def walk_spaces(homeserver: str, token: str) -> Iterator[str]:
    """Every room on the homeserver whose type is m.space, by the Admin API."""
    start = 0
    for _ in range(MAX_PAGES):
        query = urllib.parse.urlencode({"from": start, "limit": PAGE_SIZE})
        page = _request_json(f"{homeserver}{ADMIN_ROOMS_PATH}?{query}", token)
        for room in page.get("rooms", []):
            if room.get("room_type") == SPACE_ROOM_TYPE:
                yield room["room_id"]
        if "next_batch" not in page:
            return
        start = page["next_batch"]
        time.sleep(PAGE_PAUSE_SECONDS)
    raise RuntimeError(f"More than {MAX_PAGES} pages of rooms; refusing to continue")


def dry_run(homeserver: str, token: str, room_id: str) -> Dict[str, Any]:
    """The endpoint's report of what would change in one space."""
    return _request_json(
        f"{homeserver}{REPAIR_PATH}", token, body={"room_id": room_id, "dry_run": True}
    )


def remaining_work(report: Dict[str, Any]) -> bool:
    """Whether a dry-run report shows anything the sync could still change."""
    return any(
        chat["promoted"] or chat["demoted"] or chat["joined"]
        for chat in report.get("chats", [])
    )


def needs_attention(report: Dict[str, Any]) -> bool:
    """Whether a report shows anything to change, or anything the sync cannot
    fix on its own (a chat nobody can act for, a failed step)."""
    return remaining_work(report) or any(
        chat["admin_sender_missing"] or chat["failed"] or chat.get("ignored")
        for chat in report.get("chats", [])
    )


def repair(
    homeserver: str, token: str, room_id: str, *, wait_seconds: float
) -> Dict[str, Any]:
    """Repair one space and return the dry-run report once nothing is left to
    change, or once the sync has reported what it could not do. Raises when
    the wait runs out with work still pending."""
    _request_json(f"{homeserver}{REPAIR_PATH}", token, body={"room_id": room_id})
    deadline = time.monotonic() + wait_seconds
    while True:
        time.sleep(POLL_SECONDS)
        report = dry_run(homeserver, token, room_id)
        if not remaining_work(report):
            return report
        if time.monotonic() > deadline:
            raise RequestFailed(f"still {describe(report)} after {int(wait_seconds)} s")


def describe(report: Dict[str, Any]) -> str:
    parts = []
    for chat in report.get("chats", []):
        if chat.get("ignored"):
            parts.append(f"{chat['chat_id']}: IGNORED, {chat['ignored']}")
            continue
        detail = (
            f"{chat['chat_id']}: promote {len(chat['promoted'])},"
            f" demote {len(chat['demoted'])}, join {len(chat['joined'])}"
        )
        if chat["admin_sender_missing"]:
            detail += ", NO JOINED ADMIN"
        if chat["failed"]:
            detail += f", FAILED {len(chat['failed'])}"
        parts.append(detail)
    return "; ".join(parts) if parts else "no default chats"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--homeserver", required=True, help="e.g. https://matrix.staging.pangea.chat"
    )
    parser.add_argument(
        "--room-id",
        action="append",
        default=[],
        help="A space to check; repeatable. Without it, every space on the homeserver.",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Repair the spaces that need it."
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        help="How long to wait for one space's repair to finish.",
    )
    args = parser.parse_args()

    token = os.environ.get("MATRIX_ADMIN_TOKEN")
    if not token:
        print("MATRIX_ADMIN_TOKEN is not set", file=sys.stderr)
        return 2
    homeserver = args.homeserver.rstrip("/")

    try:
        spaces: List[str] = args.room_id or list(walk_spaces(homeserver, token))
    except (RequestFailed, RuntimeError) as e:
        print(f"Could not list spaces: {e}", file=sys.stderr)
        return 1
    print(f"Checking {len(spaces)} spaces")

    to_repair: List[str] = []
    check_failures = 0
    for room_id in spaces:
        try:
            report = dry_run(homeserver, token, room_id)
        except RequestFailed as e:
            check_failures += 1
            print(f"{room_id}: dry run failed: {e}")
            continue
        if needs_attention(report):
            to_repair.append(room_id)
            print(f"{room_id}: {describe(report)}")
    print(
        f"{len(to_repair)} of {len(spaces)} spaces need attention,"
        f" {check_failures} could not be checked"
    )

    if not args.apply or not to_repair:
        return 1 if check_failures else 0

    repair_failures = 0
    for room_id in to_repair:
        try:
            report = repair(homeserver, token, room_id, wait_seconds=args.wait_seconds)
        except RequestFailed as e:
            repair_failures += 1
            print(f"{room_id}: repair failed: {e}")
            continue
        if needs_attention(report):
            repair_failures += 1
        print(f"{room_id}: after repair: {describe(report)}")
    print(f"Repaired {len(to_repair)} spaces, {repair_failures} still need attention")
    return 1 if (repair_failures or check_failures) else 0


if __name__ == "__main__":
    sys.exit(main())
