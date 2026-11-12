"""The course space's Can-Do list (``pangea.course_missions``) a server-created
course starts with.

Content Studio lets a teacher reorder and trim a course's Can-Dos. The course
space's Can-Do list lives in this state event; a course without one reads the
shared quest order, exactly as before the list existed. Building a list is a
snapshot of that shared list: the quest plan's learning objectives in order,
each with the visible library activities at the course's target language that
reference it, as the client's course outline shows them. An entry that selects
activities (``activity_ids``) lists those of them the library returns, in its
order, and all of them when it returns none; the entry's note is copied too
(org quests.instructions.md, "Each entry in the Can-Do sequence").

The list is optional. A CMS read that fails leaves the course without one and
the Studio writes it on first publish, so a failure here is logged and never
blocks course creation. See create-course-space.instructions.md.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypeGuard
from urllib.parse import quote, urlencode

from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.module_api import ModuleApi
from synapse.util.async_helpers import timeout_deferred

from synapse_pangea_chat.cms_http import cms_request, service_headers
from synapse_pangea_chat.room_code.instructor_access import user_power

COURSE_MISSIONS_STATE_EVENT_TYPE = "pangea.course_missions"
# Only a course admin may write the list; a list from anyone else is ignored.
COURSE_ADMIN_POWER = 100

# The whole read (both CMS requests) is bounded: course creation and a claim
# wait on it, and a caller that times out and retries a creation would make a
# second course. A hung CMS costs at most this much.
FETCH_TIMEOUT_SECONDS = 3
# The course listing's own page size (choreo list_activity_plans_for_los).
ACTIVITY_LIMIT = 200
MAX_BODY_BYTES = 16_000_000
# Readers refuse a whole list whose note is longer, counted in UTF-16 units as
# the client and the CMS count it.
MAX_NOTE_LENGTH = 280

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.course_missions"
)


class CourseMissionsError(RuntimeError):
    """The CMS could not supply what a Can-Do list is built from."""


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


def _quest_entries(quest: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    """The quest's entries by learning objective, in its order, each once."""
    res = quest.get("res")
    steps = res.get("learning_objective_sequence") if isinstance(res, Mapping) else None
    if not isinstance(steps, list):
        return {}
    entries: Dict[str, Mapping[str, Any]] = {}
    for step in steps:
        if isinstance(step, Mapping):
            lo_id = _ref_id(step.get("learning_objective"))
            # A Mission listed twice keeps its first place: readers reject a
            # Can-Do list that names the same Mission twice.
            if lo_id is not None and lo_id not in entries:
                entries[lo_id] = step
    return entries


def quest_lo_ids(quest: Mapping[str, Any]) -> List[str]:
    """The quest's learning objectives, in its order, each once."""
    return list(_quest_entries(quest))


def _selected(entry: Mapping[str, Any]) -> List[str]:
    """The entry's selected activity ids, each once; empty for no selection
    (null and ``[]`` alike)."""
    ids = entry.get("activity_ids")
    if not isinstance(ids, list):
        return []
    return list(dict.fromkeys(a for a in ids if isinstance(a, str) and a))


def _note(entry: Mapping[str, Any], lo_id: str) -> Optional[str]:
    """The entry's note, or None when it has none. A note longer than readers
    accept is left out, so it cannot invalidate the whole list."""
    note = entry.get("note")
    if not isinstance(note, str) or not note.strip():
        return None
    if len(note.encode("utf-16-le")) // 2 > MAX_NOTE_LENGTH:
        logger.warning(
            "Can-Do list left out the quest's note for %s: longer than %d",
            lo_id,
            MAX_NOTE_LENGTH,
        )
        return None
    return note


def _visible_at(doc: Mapping[str, Any], l2: str) -> bool:
    """The library row a learner sees: canonical, not hidden, not private,
    at the course's target language. The CMS query applies the same clauses;
    they are checked again here so a looser answer cannot widen the list."""
    req = doc.get("req")
    if isinstance(req, Mapping) and req.get("source_request_hash"):
        return False
    return (
        doc.get("hidden") is not True
        and doc.get("visibility") != "private"
        and _plan(doc).get("l2") == l2
    )


def activity_lo_ids(doc: Mapping[str, Any]) -> List[str]:
    """The Missions an ``activities-v2`` row is filed under."""
    refs = doc.get("learningObjectiveRefs")
    return [
        lo_id
        for lo_id in (_ref_id(ref) for ref in (refs if isinstance(refs, list) else []))
        if lo_id is not None
    ]


def _string_list(value: Any) -> TypeGuard[List[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _missions_lo_ids(content: Mapping[str, Any]) -> Optional[List[str]]:
    """Every Mission a Can-Do list names, shown and removed, or None when the
    list is malformed. The client's strict parse (``CourseMissionsEvent
    .tryParse``) decides which records count; this checks the same structure
    (version, revision, each Mission's id and activity lists, the removed
    Missions), so a list the client ignores names nothing here. A note, due
    date or time zone the client would refuse is not checked: none of them
    changes which Missions the course lists."""
    revision = content.get("revision")
    missions = content.get("missions")
    if (
        content.get("version") != 1
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(missions, list)
    ):
        return None
    shown: List[str] = []
    for mission in missions:
        if not isinstance(mission, Mapping):
            return None
        lo_id = mission.get("lo_id")
        removed_ids = mission.get("removed_activity_ids")
        if (
            not isinstance(lo_id, str)
            or not lo_id
            or lo_id in shown
            or not _string_list(mission.get("activity_ids"))
            or (removed_ids is not None and not _string_list(removed_ids))
        ):
            return None
        shown.append(lo_id)
    removed: List[str] = []
    removed_missions = content.get("removed_missions")
    if removed_missions is not None:
        if not isinstance(removed_missions, list):
            return None
        for mission in removed_missions:
            if not isinstance(mission, Mapping):
                return None
            lo_id = mission.get("lo_id")
            ids = mission.get("activity_ids")
            shown_count = mission.get("shown_count")
            if (
                not isinstance(lo_id, str)
                or not lo_id
                or not _string_list(ids)
                or not isinstance(shown_count, int)
                or isinstance(shown_count, bool)
                or not 0 <= shown_count <= len(ids)
            ):
                return None
            removed.append(lo_id)
    return shown + [lo_id for lo_id in removed if lo_id not in shown]


def course_missions_lo_ids(state: Mapping[Tuple[str, str], Any]) -> List[str]:
    """The Missions the course space's Can-Do list names, as the client reads
    it: empty unless the course has a well-formed ``pangea.course_missions``
    sent by someone who is a course admin (power level 100, creators count)
    now, so a student can never add a Mission to their course. ``state`` must
    hold the list, the create event and the power levels."""
    event = state.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
    # Empty content is the list a failed read leaves (set_course_plan):
    # no list, by design.
    if event is None or not event.content:
        return []
    if user_power(state, event.sender) < COURSE_ADMIN_POWER:
        logger.info(
            "Can-Do list in %s was not sent by a course admin; ignored",
            getattr(event, "room_id", "?"),
        )
        return []
    lo_ids = _missions_lo_ids(event.content)
    if lo_ids is None:
        logger.warning(
            "Can-Do list in %s is malformed; ignored, as the client does",
            getattr(event, "room_id", "?"),
        )
        return []
    return lo_ids


def build_course_missions_content(
    quest: Mapping[str, Any], activities: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """The Can-Do list: each learning objective in quest order with its activities.

    ``activities`` are ``activities-v2`` rows in the listing's order. Each
    activity is identified by its content id (``res.plan.activity_id``), never
    its row id, and the first row per activity wins, as the client dedupes.
    An entry's selection keeps those of its activities the listing returns, in
    the selection's order; one the listing returns none of keeps them all, so
    a stale selection never empties a Can-Do.
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
        for lo_id in activity_lo_ids(doc):
            by_lo.setdefault(lo_id, []).append(activity_id)

    missions: List[Dict[str, Any]] = []
    for lo_id, entry in _quest_entries(quest).items():
        listed = by_lo.get(lo_id, [])
        available = set(listed)
        chosen = [a for a in _selected(entry) if a in available]
        mission: Dict[str, Any] = {"lo_id": lo_id, "activity_ids": chosen or listed}
        note = _note(entry, lo_id)
        if note is not None:
            mission["note"] = note
        missions.append(mission)
    return {"version": 1, "revision": 1, "missions": missions}


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
        self._headers = service_headers(cms_api_key)

    async def _get(self, path: str) -> Any:
        code, raw = await cms_request(
            self._http,
            "GET",
            self._base + path,
            self._headers,
            max_bytes=MAX_BODY_BYTES,
        )
        if code != 200:
            raise CourseMissionsError(f"CMS {path.split('?')[0]} answered {code}")
        return json.loads(raw)

    async def _quest(self, quest_id: str) -> Mapping[str, Any]:
        quest = await self._get(f"quest-plans/{quote(quest_id, safe='')}?depth=0")
        if not isinstance(quest, Mapping):
            raise CourseMissionsError("quest read returned no document")
        return quest

    async def quest_lo_ids(self, quest_id: str) -> List[str]:
        """The quest's Missions, in order; raises when the CMS cannot say."""
        return quest_lo_ids(await self._quest(quest_id))

    async def _build(self, quest_id: str) -> Dict[str, Any]:
        quest = await self._quest(quest_id)
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
        """The Can-Do list for ``quest_id``, or None when it cannot be built."""
        if not quest_id:
            logger.warning("No Can-Do list written: the request names no quest")
            return None
        try:
            return await make_deferred_yieldable(
                timeout_deferred(
                    deferred=run_in_background(self._build, quest_id),
                    timeout=FETCH_TIMEOUT_SECONDS,
                    clock=self._clock,
                )
            )
        # silent-ok: the Can-Do list is optional; without it the course reads the shared quest order and the Studio writes it on first publish
        except Exception as e:
            logger.warning(
                "No Can-Do list written for quest %s: %s: %s",
                quest_id,
                type(e).__name__,
                e,
            )
            return None
