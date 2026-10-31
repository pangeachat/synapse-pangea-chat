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
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
)
from urllib.parse import parse_qsl, quote, urlencode

from synapse.http.client import read_body_with_max_size
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.module_api import ModuleApi
from synapse.util.async_helpers import timeout_deferred
from twisted.web.http_headers import Headers

COURSE_MISSIONS_STATE_EVENT_TYPE = "pangea.course_missions"
# The ship's position on an undated trip, written by the course_trip pass.
# Defined here beside the copy, not in course_trip, so the public catalog can
# read both without importing course_trip, which imports the catalog.
COURSE_POSITION_STATE_EVENT_TYPE = "pangea.course_position"

# The whole read (both CMS requests) is bounded: course creation and a claim
# wait on it, and a caller that times out and retries a creation would make a
# second course. A hung CMS costs at most this much.
FETCH_TIMEOUT_SECONDS = 3
# The course listing's own page size (choreo list_activity_plans_for_los).
ACTIVITY_LIMIT = 200
# The one-time slice of an existing course (an operator call, not a creation)
# reads the whole library and every listed activity's position, so it gets
# its own, longer bound and a page cap it refuses to exceed.
SLICE_READ_TIMEOUT_SECONDS = 60
SLICE_MAX_PAGES = 50
# Activities whose positions one by-id read asks for.
POINTS_BATCH = 100
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
    """The quest's learning objectives, in its order, each once."""
    res = quest.get("res")
    steps = res.get("learning_objective_sequence") if isinstance(res, Mapping) else None
    if not isinstance(steps, list):
        return []
    ids: List[str] = []
    for step in steps:
        if isinstance(step, Mapping):
            lo_id = _ref_id(step.get("learning_objective"))
            # A Mission listed twice keeps its first place: readers reject a
            # copy that names the same Mission twice.
            if lo_id is not None and lo_id not in ids:
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


def activity_places(
    docs: Sequence[Mapping[str, Any]],
) -> Dict[str, Optional[str]]:
    """Each placed activity's city key, as the CMS stores it (``city_key``,
    cms cities.instructions.md), or None for one placed under no listed city.
    An activity with no usable position in ``res.plan.coordinates`` (stored
    ``[lng, lat]``; ``[0, 0]`` is the unplaced sentinel) is left out: its
    place is unknown, which a slice treats differently from no city."""
    places: Dict[str, Optional[str]] = {}
    for doc in docs:
        if not isinstance(doc, Mapping):
            continue
        plan = _plan(doc)
        activity_id = plan.get("activity_id")
        coordinates = plan.get("coordinates")
        if not isinstance(activity_id, str) or not activity_id:
            continue
        if not isinstance(coordinates, list) or len(coordinates) != 2:
            continue
        lng, lat = coordinates
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lat, lng)
        ):
            continue
        if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
            continue
        key = doc.get("city_key")
        places.setdefault(activity_id, key if isinstance(key, str) and key else None)
    return places


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
    def __init__(
        self,
        api: ModuleApi,
        cms_base_url: str,
        cms_api_key: str,
        slicer: Optional[Callable[[], Awaitable[Any]]] = None,
    ):
        self._http = api.http_client
        self._clock = api._hs.get_clock()
        self._base = cms_base_url.rstrip("/") + "/api/"
        self._headers = Headers(
            {"Authorization": [f"service-users API-Key {cms_api_key}"]}
        )
        #: When set (course_missions_v2_enabled), a new course's copy is the
        #: trip: ``slicer()`` answers a ``TripSlicer`` over the current city
        #: list (None when the list cannot be read), and the copy is sliced
        #: before it is written.
        self._slicer = slicer

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

    async def _build_with_docs(
        self, quest_id: str
    ) -> "tuple[Dict[str, Any], List[Mapping[str, Any]]]":
        quest = await self._get(f"quest-plans/{quote(quest_id, safe='')}?depth=0")
        if not isinstance(quest, Mapping):
            raise CourseMissionsError("quest read returned no document")
        l2 = quest_target_language(quest)
        lo_ids = quest_lo_ids(quest)
        if l2 is None:
            raise CourseMissionsError("quest has no target language")
        if not lo_ids:
            return build_course_missions_content(quest, []), []
        found = await self._get("activities-v2?" + activities_query(lo_ids, l2))
        docs = found.get("docs") if isinstance(found, Mapping) else None
        if not isinstance(docs, list):
            raise CourseMissionsError("activities read returned no docs list")
        return build_course_missions_content(quest, docs), docs

    async def _build(self, quest_id: str) -> Dict[str, Any]:
        content, docs = await self._build_with_docs(quest_id)
        slicer = self._slicer
        if slicer is None:
            return content
        # The trip (course-trip.instructions.md): each Can-Do sliced to its
        # city, from the same documents. Without the city list (``None``,
        # logged by the slicer) the course still gets its own copy, unsliced,
        # and the slice pass gives it cities later.
        trip = await slicer()
        if trip is None:
            return content
        copy, _ = trip.slice_copy(content, activity_places(docs), None)
        return copy

    async def _all_docs(self, collection: str, params: List[Any]) -> List[Any]:
        """Every row a query matches, a page at a time; refuses rather than
        answer with part of the result."""
        docs: List[Any] = []
        for page in range(1, SLICE_MAX_PAGES + 1):
            found = await self._get(
                f"{collection}?" + urlencode([*params, ("page", str(page))])
            )
            rows = found.get("docs") if isinstance(found, Mapping) else None
            if not isinstance(rows, list):
                raise CourseMissionsError(f"{collection} read returned no docs list")
            docs.extend(rows)
            if found.get("hasNextPage") is not True:
                return docs
        raise CourseMissionsError(
            f"{collection} read did not finish within {SLICE_MAX_PAGES} pages"
        )

    async def _complete_with_docs(
        self, quest_id: str, listed: Sequence[str]
    ) -> "tuple[Dict[str, Any], Dict[str, Optional[str]]]":
        quest = await self._get(f"quest-plans/{quote(quest_id, safe='')}?depth=0")
        if not isinstance(quest, Mapping):
            raise CourseMissionsError("quest read returned no document")
        l2 = quest_target_language(quest)
        lo_ids = quest_lo_ids(quest)
        if l2 is None:
            raise CourseMissionsError("quest has no target language")
        docs: List[Any] = []
        if lo_ids:
            docs = await self._all_docs(
                "activities-v2", parse_qsl(activities_query(lo_ids, l2))
            )
        places = activity_places(docs)
        # A course's own list can name activities the library listing does
        # not return (a teacher's copies, rows past the listing's page): their
        # cities are read by id, so each is placed at its real city.
        missing = [a for a in dict.fromkeys(listed) if a not in places]
        for start in range(0, len(missing), POINTS_BATCH):
            batch = missing[start : start + POINTS_BATCH]
            rows = await self._all_docs(
                "activities-v2",
                [
                    ("where[res.plan.activity_id][in]", ",".join(batch)),
                    ("limit", str(POINTS_BATCH)),
                    ("depth", "0"),
                    ("select[res][plan][activity_id]", "true"),
                    ("select[res][plan][coordinates]", "true"),
                    ("select[city_key]", "true"),
                ],
            )
            for activity_id, key in activity_places(rows).items():
                places.setdefault(activity_id, key)
        return build_course_missions_content(quest, docs), places

    async def read_for_slice(
        self, quest_id: str, listed: Sequence[str] = ()
    ) -> "tuple[Dict[str, Any], Dict[str, Optional[str]]]":
        """For the one-time slice: the quest's whole library as a version 1
        copy, and the city of every placed activity it lists or the course
        lists (``listed``). Raises :class:`CourseMissionsError` when either cannot be
        read in full (unlike :meth:`read`, which answers None)."""
        return await make_deferred_yieldable(
            timeout_deferred(
                deferred=run_in_background(
                    self._complete_with_docs, quest_id, list(listed)
                ),
                timeout=SLICE_READ_TIMEOUT_SECONDS,
                clock=self._clock,
            )
        )

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
