"""The course copy (``pangea.course_missions``) a server-created course starts with.

Content Studio lets a teacher reorder and trim a course's Can-Dos. The course's
own copy of its outline lives in this state event; a course without one reads
the shared quest order, exactly as before the copy existed. Building a copy is
a snapshot of that shared list: the quest plan's learning objectives in order,
each with the visible library activities at the course's target language that
reference it, as the client's course outline shows them.

The copy is optional. A CMS read that fails leaves the course without one and
the Studio writes it on first publish, so a failure here is logged and never
blocks course creation. See create-course-space.instructions.md.
"""

from __future__ import annotations

import json
import logging
from io import BytesIO
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote, urlencode

from synapse.http.client import read_body_with_max_size
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.module_api import ModuleApi
from synapse.util.async_helpers import timeout_deferred
from twisted.web.http_headers import Headers

COURSE_MISSIONS_STATE_EVENT_TYPE = "pangea.course_missions"

# The whole read is bounded: the caller waits on course creation, and a caller
# that times out and retries would create a second course.
FETCH_TIMEOUT_SECONDS = 10
# The course listing's own page size (choreo list_activity_plans_for_los).
ACTIVITY_LIMIT = 200
MAX_BODY_BYTES = 16_000_000

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.course_missions"
)


class CourseMissionsError(RuntimeError):
    """The CMS could not supply what a copy is built from."""


def _ref_id(value: Any) -> Optional[str]:
    """A relationship value at depth 0 is a bare id; populated, an object."""
    if isinstance(value, Mapping):
        value = value.get("id")
    if isinstance(value, str) and value:
        return value
    return None


def _plan(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    res = doc.get("res")
    plan = res.get("plan") if isinstance(res, Mapping) else None
    return plan if isinstance(plan, Mapping) else {}


def quest_target_language(quest: Mapping[str, Any]) -> Optional[str]:
    req = quest.get("req")
    l2 = req.get("target_language") if isinstance(req, Mapping) else None
    return l2 if isinstance(l2, str) and l2 else None


def quest_lo_ids(quest: Mapping[str, Any]) -> List[str]:
    """The quest's learning objectives, in its order."""
    res = quest.get("res")
    steps = res.get("learning_objective_sequence") if isinstance(res, Mapping) else None
    if not isinstance(steps, list):
        return []
    ids = []
    for step in steps:
        if isinstance(step, Mapping):
            lo_id = _ref_id(step.get("learning_objective"))
            if lo_id is not None:
                ids.append(lo_id)
    return ids


def _visible_at(doc: Mapping[str, Any], l2: str) -> bool:
    """The library row a learner sees: canonical, not hidden, not private,
    at the course's target language. The CMS query applies the same clauses;
    they are checked again here so a looser answer cannot widen the copy."""
    req = doc.get("req")
    if isinstance(req, Mapping) and req.get("source_request_hash"):
        return False
    return (
        doc.get("hidden") is not True
        and doc.get("visibility") != "private"
        and _plan(doc).get("l2") == l2
    )


def build_course_missions_content(
    quest: Mapping[str, Any], activities: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """The copy: each learning objective in quest order with its activities.

    ``activities`` are ``activities-v2`` rows in the listing's order. Each
    activity is identified by its content id (``res.plan.activity_id``), never
    its row id, and the first row per activity wins, as the client dedupes.
    """
    l2 = quest_target_language(quest)
    if l2 is None:
        raise CourseMissionsError("quest has no target language")

    by_lo: Dict[str, List[str]] = {}
    seen: set[str] = set()
    for doc in activities:
        if not isinstance(doc, Mapping) or not _visible_at(doc, l2):
            continue
        activity_id = _plan(doc).get("activity_id")
        if not isinstance(activity_id, str) or not activity_id or activity_id in seen:
            continue
        seen.add(activity_id)
        refs = doc.get("learningObjectiveRefs")
        for ref in refs if isinstance(refs, list) else []:
            lo_id = _ref_id(ref)
            if lo_id is not None:
                by_lo.setdefault(lo_id, []).append(activity_id)

    return {
        "version": 1,
        "revision": 1,
        "missions": [
            {"lo_id": lo_id, "activity_ids": list(by_lo.get(lo_id, []))}
            for lo_id in quest_lo_ids(quest)
        ],
    }


def activities_query(lo_ids: Sequence[str], l2: str) -> str:
    """The ``activities-v2`` query the course listing runs (choreo
    list_activity_plans_for_los), without the course-member private rows."""
    params = [
        ("where[and][0][req.source_request_hash][exists]", "false"),
        ("where[and][1][hidden][not_equals]", "true"),
        ("where[and][2][visibility][not_equals]", "private"),
        ("where[and][3][res.plan.l2][equals]", l2),
    ]
    params += [
        (f"where[and][4][or][{i}][learningObjectiveRefs][contains]", lo_id)
        for i, lo_id in enumerate(lo_ids)
    ]
    params += [
        ("limit", str(ACTIVITY_LIMIT)),
        ("sort", "-updatedAt"),
        ("depth", "0"),
    ]
    return urlencode(params)


class CourseMissionsReader:
    def __init__(self, api: ModuleApi, cms_base_url: str, cms_api_key: str):
        self._http = api.http_client
        self._clock = api._hs.get_clock()
        self._base = cms_base_url.rstrip("/") + "/api/"
        self._headers = Headers(
            {"Authorization": [f"service-users API-Key {cms_api_key}"]}
        )

    async def _get(self, path: str) -> Any:
        response = await self._http.request(
            "GET", self._base + path, headers=self._headers
        )
        buffer = BytesIO()
        await make_deferred_yieldable(
            read_body_with_max_size(response, buffer, MAX_BODY_BYTES)
        )
        if response.code != 200:
            raise CourseMissionsError(
                f"CMS {path.split('?')[0]} answered {response.code}"
            )
        return json.loads(buffer.getvalue())

    async def _build(self, quest_id: str) -> Dict[str, Any]:
        quest = await self._get(f"quest-plans/{quote(quest_id, safe='')}?depth=0")
        if not isinstance(quest, Mapping):
            raise CourseMissionsError("quest read returned no document")
        l2 = quest_target_language(quest)
        lo_ids = quest_lo_ids(quest)
        if l2 is None:
            raise CourseMissionsError("quest has no target language")
        if not lo_ids:
            return build_course_missions_content(quest, [])
        found = await self._get("activities-v2?" + activities_query(lo_ids, l2))
        docs = found.get("docs") if isinstance(found, Mapping) else None
        if not isinstance(docs, list):
            raise CourseMissionsError("activities read returned no docs list")
        return build_course_missions_content(quest, docs)

    async def read(self, quest_id: str) -> Optional[Dict[str, Any]]:
        """The copy for ``quest_id``, or None when it cannot be built."""
        if not quest_id:
            logger.warning("No course copy written: the request names no quest")
            return None
        try:
            return await make_deferred_yieldable(
                timeout_deferred(
                    deferred=run_in_background(self._build, quest_id),
                    timeout=FETCH_TIMEOUT_SECONDS,
                    clock=self._clock,
                )
            )
        # silent-ok: the copy is optional; without it the course reads the shared quest order and the Studio writes it on first publish
        except Exception as e:
            logger.warning(
                "No course copy written for quest %s: %s: %s",
                quest_id,
                type(e).__name__,
                e,
            )
            return None
