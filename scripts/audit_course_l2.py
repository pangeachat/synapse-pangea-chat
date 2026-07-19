#!/usr/bin/env python3
"""Report which published courses are missing a target language, and why.

A course is filterable in Browse only if its ``pangea.course_plan`` room state
carries an ``l2``. The one-time backfill writes that from the CMS, and skips any
room whose plan it cannot resolve — deliberately, because a guessed language is
worse than none: the backfill treats any non-empty ``l2`` as already-correct, so
a wrong value is permanent.

This script answers the question that skip leaves open — *which* plans failed and
*why* — by walking the catalog, grouping the affected rooms by plan id, and
asking the CMS about each one. It is READ-ONLY. It writes nothing to Matrix or
the CMS, because the fix depends on the answer: a plan that exists but carries no
language is a CMS field to fill in (after which the backfill repairs the rooms on
its next run), while a plan that does not exist at all is a content decision.

Usage:
    MATRIX_TOKEN=... CMS_TOKEN=... python scripts/audit_course_l2.py \
        --homeserver https://matrix.staging.pangea.chat \
        --cms-url https://api.staging.pangea.chat/cms

``MATRIX_TOKEN`` is an access token for any authenticated user — the catalog is
not admin-gated. See the client repo's ``matrix-auth.instructions.md`` for how to
obtain one for staging. A token rather than a username/password pair, so this
script never handles a credential.

``CMS_TOKEN`` is a CMS ``service-users`` API key — the same value the
choreographer uses, and the same auth collection the module's own lookup uses.
``users`` is the wrong collection and 403s; that mistake is what made the old
catalog silently fall back to unfiltered results.

Neither credential is ever printed.

``--cms-url`` includes ``/cms`` (this appends ``/api/<collection>``), matching
the choreographer and the Synapse module rather than the bot and client, which
take the bare origin. See the org ``local-stack.instructions.md``.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

CATALOG_PATH = "/_synapse/client/pangea/v1/public_courses"
PAGE_SIZE = 50
PAGE_PAUSE_SECONDS = 0.3
MAX_PAGES = 200

# Newest content model first, matching the backfill's own lookup order. A course
# space pins a quest-plans row id; the retired v1 course-plans collection is only
# a fallback for rooms old enough to reference it.
PLAN_SOURCES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("quest-plans", ("req", "target_language")),
    ("course-plans", ("l2",)),
)

STATUS_NO_PLAN = "plan not found in either collection"
STATUS_NO_LANGUAGE = "plan found but carries no language"


def _get_json(url: str, headers: Dict[str, str], timeout: int = 30) -> Any:
    request = urllib.request.Request(url)
    for key, value in headers.items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _dig(doc: Dict[str, Any], path: Sequence[str]) -> Any:
    value: Any = doc
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def walk_catalog(homeserver: str, token: str) -> List[Dict[str, Any]]:
    """Every published course, following the catalog's keyset cursor."""
    headers = {"Authorization": f"Bearer {token}"}
    courses: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    for _ in range(MAX_PAGES):
        query = f"limit={PAGE_SIZE}"
        if cursor:
            query += "&since=" + urllib.parse.quote(cursor, safe="")
        page = _get_json(f"{homeserver}{CATALOG_PATH}?{query}", headers)
        chunk = page.get("chunk", [])
        courses.extend(chunk)
        cursor = page.get("next_batch")
        if not cursor or not chunk:
            break
        time.sleep(PAGE_PAUSE_SECONDS)

    return courses


def resolve_plan_languages(
    plan_ids: Sequence[str], cms_url: str, cms_key: str
) -> Dict[str, Optional[str]]:
    """plan id -> language, or ``None`` when the CMS has the row but no language.

    Ids absent from the result did not resolve in either collection at all. The
    distinction matters: it is the difference between "fill in a field" and
    "this plan does not exist".
    """
    headers = {"Authorization": f"service-users API-Key {cms_key}"}
    found: Dict[str, Optional[str]] = {}
    outstanding = [plan_id for plan_id in dict.fromkeys(plan_ids) if plan_id]

    for collection, language_path in PLAN_SOURCES:
        if not outstanding:
            break
        query = urllib.parse.urlencode(
            {
                "where[id][in]": ",".join(outstanding),
                "limit": str(len(outstanding)),
                "depth": "0",
            }
        )
        docs = _get_json(f"{cms_url}/api/{collection}?{query}", headers).get("docs", [])
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            plan_id = doc.get("id")
            if not plan_id:
                continue
            language = _dig(doc, language_path)
            found[str(plan_id)] = (
                language.strip()
                if isinstance(language, str) and language.strip()
                else None
            )
        outstanding = [plan_id for plan_id in outstanding if plan_id not in found]

    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--homeserver", required=True)
    parser.add_argument("--cms-url", required=True, help="CMS base including /cms")
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output instead of a table"
    )
    args = parser.parse_args()

    token = os.environ.get("MATRIX_TOKEN")
    cms_key = os.environ.get("CMS_TOKEN")
    if not token or not cms_key:
        print("MATRIX_TOKEN and CMS_TOKEN must both be set", file=sys.stderr)
        return 2

    courses = walk_catalog(args.homeserver.rstrip("/"), token)
    missing = [c for c in courses if not (c.get("target_language") or "").strip()]

    if not missing:
        print(f"All {len(courses)} published courses carry a language. Nothing to do.")
        return 0

    by_plan: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for course in missing:
        by_plan[str(course.get("course_id"))].append(course)

    resolved = resolve_plan_languages(list(by_plan), args.cms_url.rstrip("/"), cms_key)

    rows = []
    for plan_id, rooms in sorted(
        by_plan.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        if plan_id not in resolved:
            status = STATUS_NO_PLAN
        elif resolved[plan_id] is None:
            status = STATUS_NO_LANGUAGE
        else:
            # The CMS knows the language, so the backfill should have written it.
            # Either it has not run since, or the write failed for these rooms.
            status = f"resolvable ({resolved[plan_id]}) — re-run the backfill"
        rows.append(
            {
                "plan_id": plan_id,
                "status": status,
                "rooms": len(rooms),
                "sample": [r.get("name") or r.get("room_id") for r in rooms[:3]],
            }
        )

    if args.json:
        print(
            json.dumps(
                {"total": len(courses), "missing": len(missing), "plans": rows},
                indent=2,
            )
        )
        return 0

    pct = len(missing) / len(courses) * 100
    print(
        f"\n{len(missing)} of {len(courses)} published courses ({pct:.1f}%) have no "
        f"language, across {len(rows)} distinct plans.\n"
    )
    print(f"  {'plan id':38}  {'rooms':>5}  status")
    print(f"  {'-' * 38}  {'-' * 5}  {'-' * 44}")
    for row in rows:
        print(f"  {row['plan_id']:38}  {row['rooms']:>5}  {row['status']}")
    print("\nWhat to do with each:")
    print(f"  '{STATUS_NO_LANGUAGE}'  → set the language in the CMS; the backfill")
    print("     repairs the rooms on its next run.")
    print(f"  '{STATUS_NO_PLAN}'  → the rooms reference a plan that is gone.")
    print("     A content decision, not a data fix — these courses cannot be filtered.")
    print("\nNothing was written. This script only reports.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
