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
``--apply`` it then repairs exactly those, one call per space, and prints each
space's report. Run it without ``--apply`` first and read the list.

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


def _request_json(
    url: str, token: str, body: Optional[Dict[str, Any]] = None, timeout: int = 300
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if body else "GET")
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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


def sync_space(
    homeserver: str, token: str, room_id: str, *, apply: bool
) -> Dict[str, Any]:
    """The endpoint's report for one space: what would change, or what did."""
    return _request_json(
        f"{homeserver}{REPAIR_PATH}",
        token,
        body={"room_id": room_id, "dry_run": not apply},
    )


def needs_repair(report: Dict[str, Any]) -> bool:
    """Whether a dry-run report shows anything to change in any chat."""
    return any(
        chat["promoted"]
        or chat["demoted"]
        or chat["joined"]
        or chat["admin_sender_missing"]
        for chat in report.get("chats", [])
    )


def describe(report: Dict[str, Any]) -> str:
    parts = []
    for chat in report.get("chats", []):
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
    args = parser.parse_args()

    token = os.environ.get("MATRIX_ADMIN_TOKEN")
    if not token:
        print("MATRIX_ADMIN_TOKEN is not set", file=sys.stderr)
        return 2
    homeserver = args.homeserver.rstrip("/")

    spaces: List[str] = args.room_id or list(walk_spaces(homeserver, token))
    print(f"Checking {len(spaces)} spaces")
    to_repair: List[str] = []
    for room_id in spaces:
        try:
            report = sync_space(homeserver, token, room_id, apply=False)
        except urllib.error.HTTPError as e:
            print(
                f"{room_id}: dry run failed with HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"
            )
            continue
        if needs_repair(report):
            to_repair.append(room_id)
            print(f"{room_id}: {describe(report)}")
    print(f"{len(to_repair)} of {len(spaces)} spaces need repair")

    if not args.apply or not to_repair:
        return 0

    failures = 0
    for room_id in to_repair:
        try:
            report = sync_space(homeserver, token, room_id, apply=True)
        except urllib.error.HTTPError as e:
            failures += 1
            print(
                f"{room_id}: repair failed with HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"
            )
            continue
        if any(
            chat["failed"] or chat["admin_sender_missing"] for chat in report["chats"]
        ):
            failures += 1
        print(f"{room_id}: repaired: {describe(report)}")
    print(f"Repaired {len(to_repair)} spaces, {failures} with failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
