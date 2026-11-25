"""The course space's Can-Do list (``pangea.course_missions``) a server-created
course starts with.

Content Studio lets a teacher reorder and trim a course's Can-Dos. The course
space's Can-Do list lives in this state event. Building a list is a snapshot of
the quest: its learning objectives in order, each with the quest entry's note
and the selected activities (``activity_ids``) that are listed: visible library
activities at the course's target language filed under the entry's Can-Do, in
the selection's order (org quests.instructions.md, "Each entry in the Can-Do
sequence").

The library read is the course listing's: the newest ``ACTIVITY_LIMIT`` rows
for all the quest's Can-Dos together. A merged Can-Do carries hundreds of
activities, so that page can miss a selection entirely (pangeachat/.github#832);
the selected activities it missed are therefore read by id, under the same
clauses, as the course listing does (2-step-choreographer#3523).

An entry with nothing to show pins its Can-Do's ranked ten
(``rank_activities``), the short list the quests doc says such an entry shows,
read for that Can-Do by score. What the snapshot cannot carry over is never
dropped quietly:

- An entry whose selected activities are all hidden, deleted or missing pins
  the ranked ten; captured as an error. One that keeps some of its selection
  keeps those and pins nothing extra; captured as one warning per build.
- An entry that selects nothing (unset or empty) pins the ranked ten;
  captured as one warning per build. A teacher's own quest from Create my
  class selects nothing, so this is expected there.
- A note longer than readers accept is shortened at a word boundary; captured
  as an error, since the CMS limits the field.
- A list that cannot be built raises ``CourseMissionsError`` with a ``code``;
  each caller decides what the course does without it and captures it.

See create-course-space.instructions.md.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import (
    Any,
    Collection,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeGuard,
)
from urllib.parse import quote, urlencode

from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.module_api import ModuleApi
from synapse.util.async_helpers import (
    concurrently_execute,
    timeout_deferred,
    yieldable_gather_results,
)
from twisted.internet import defer

from synapse_pangea_chat.cms_http import cms_request, service_headers
from synapse_pangea_chat.room_code.instructor_access import user_power
from synapse_pangea_chat.sentry_capture import capture_message

COURSE_MISSIONS_STATE_EVENT_TYPE = "pangea.course_missions"
# Only a course admin may write the list; a list from anyone else is ignored.
COURSE_ADMIN_POWER = 100

# The whole read (both CMS requests) is bounded: course creation and a claim
# wait on it, and a caller that times out and retries a creation would make a
# second course. A hung CMS costs at most this much.
FETCH_TIMEOUT_SECONDS = 3
# The course listing's own page size (choreo list_activity_plans_for_los).
ACTIVITY_LIMIT = 200
# Selected activities the page missed are read by id this many at a time, as
# choreo does (_ID_LOOKUP_CHUNK), keeping each query string short.
SELECTED_CHUNK = 40
# A chunk's rows beyond one per id are duplicate canonical rows; more than this
# many per id refuses the read rather than cut it short.
SELECTED_ROWS_PER_ID = 5
MAX_BODY_BYTES = 16_000_000
# Readers refuse a whole list whose note is longer, counted in UTF-16 units as
# the client and the CMS count it.
MAX_NOTE_LENGTH = 280
NOTE_ELLIPSIS = "\u2026"
# A failed first attempt is tried once more only with at least this much of
# FETCH_TIMEOUT_SECONDS left.
MIN_RETRY_SECONDS = 0.5
# The ranked list an entry with nothing to show pins, kept in step with the
# org quests doc ("Each entry in the Can-Do sequence": "at most about ten, at
# or near the quest's level, best-scored first"). Ten, best audit score
# first; activities scored at or below LOW_SCORE only when the Can-Do has
# nothing else; between equal scores, one at the quest's own level first. No
# rule for a level near the quest's is applied beyond that tie-break.
RANKED_COUNT = 10
LOW_SCORE = 2
# Can-Dos whose ranked rows are read at once: a quest that selects nothing
# needs them for every Can-Do, and the CMS has slow spells.
RANKED_CONCURRENCY = 6
# Of the read's budget, kept back from the ranked reads for the rest of the
# build, so running out of time leaves a list rather than a timeout.
RANKED_MARGIN_SECONDS = 0.25

# Why no list could be built (CourseMissionsError.code).
NO_QUEST = "no_quest"
QUEST_NOT_FOUND = "quest_not_found"
QUEST_MALFORMED = "quest_malformed"
CMS_UNAVAILABLE = "cms_unavailable"
CMS_REFUSED = "cms_refused"
CMS_MALFORMED = "cms_malformed"
TIMEOUT = "timeout"
# The failures a later attempt can get past; the others need a person.
RETRYABLE = frozenset({CMS_UNAVAILABLE, TIMEOUT})

# What a built list could not carry over from its quest (Problem.code), and
# the Sentry level each is captured at.
SELECTION_ALL_GONE = "selection_all_gone"
SELECTION_PARTLY_GONE = "selection_partly_gone"
SELECTION_UNSET = "selection_unset"
RANKED_UNFINISHED = "ranked_unfinished"
NOTE_TOO_LONG = "note_too_long"
PROBLEM_LEVELS = {
    SELECTION_ALL_GONE: "error",
    # Known while quests are repaired: visible, not an error.
    SELECTION_PARTLY_GONE: "warning",
    # Every entry of a teacher's own quest: expected, so a warning.
    SELECTION_UNSET: "warning",
    # The CMS was too slow for the budget; a later build replaces these.
    RANKED_UNFINISHED: "error",
    NOTE_TOO_LONG: "error",
}

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.course_missions"
)


class CourseMissionsError(RuntimeError):
    """No Can-Do list could be built; ``code`` says why (the module constants
    above), so a caller can tell a quest that is gone from a CMS that is down."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Problem:
    """Something a built list could not carry over from one quest entry.
    Ids and counts only: never a note's text."""

    code: str
    lo_id: str
    counts: Mapping[str, int]


@dataclass(frozen=True)
class BuiltCourseMissions:
    """``unfinished``: the Can-Dos whose ranked rows were not read in time.
    Their pins are ranked from the rows in hand (the newest library page), so
    the list is whole but those entries are worth building again."""

    content: Dict[str, Any]
    problems: List[Problem] = field(default_factory=list)
    unfinished: List[str] = field(default_factory=list)


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


def quest_level(quest: Mapping[str, Any]) -> Optional[str]:
    req = quest.get("req")
    level = req.get("target_cefr") if isinstance(req, Mapping) else None
    return level if isinstance(level, str) and level else None


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
            # Can-Do list that names the same Can-Do twice.
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


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def shorten_note(note: str) -> str:
    """``note`` cut to fit MAX_NOTE_LENGTH with the ellipsis: at the last
    word boundary that fits, or mid-word when the note has none. Never
    splits a character, as Python strings hold whole code points."""
    room = MAX_NOTE_LENGTH - utf16_length(NOTE_ELLIPSIS)
    end = 0
    used = 0
    for char in note:
        used += utf16_length(char)
        if used > room:
            break
        end += 1
    head = note[:end]
    if end < len(note) and not note[end].isspace():
        boundary = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
        if boundary > 0:
            head = head[:boundary]
    return head.rstrip() + NOTE_ELLIPSIS


def _note(entry: Mapping[str, Any], lo_id: str) -> Tuple[Optional[str], List[Problem]]:
    """The entry's note, or None when it has none. A note longer than readers
    accept would invalidate the whole list, so it is shortened instead."""
    note = entry.get("note")
    if not isinstance(note, str) or not note.strip():
        return None, []
    length = utf16_length(note)
    if length <= MAX_NOTE_LENGTH:
        return note, []
    return shorten_note(note), [Problem(NOTE_TOO_LONG, lo_id, {"length": length})]


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


def _can_do_list_lo_ids(content: Mapping[str, Any]) -> Optional[List[str]]:
    """Every Can-Do a list names, shown and removed, or None when the
    list is malformed. The client's strict parse (``CourseMissionsEvent
    .tryParse``) decides which records count; this checks the same structure
    (version, revision, each Can-Do's id and activity lists, the removed
    Can-Dos), so a list the client ignores names nothing here. A note, due
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


def can_do_list_lo_ids(state: Mapping[Tuple[str, str], Any]) -> List[str]:
    """The Can-Dos the course space's Can-Do list names, as the client reads
    it: empty unless the course has a well-formed ``pangea.course_missions``
    sent by someone who is a course admin (power level 100, creators count)
    now, so a student can never add a Can-Do to their course. ``state`` must
    hold the list, the create event and the power levels."""
    event = state.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
    # Empty content is the list a failed read used to leave (set_course_plan,
    # before it refused instead); rooms may still hold one: no list.
    if event is None or not event.content:
        return []
    if user_power(state, event.sender) < COURSE_ADMIN_POWER:
        logger.info(
            "Can-Do list in %s was not sent by a course admin; ignored",
            getattr(event, "room_id", "?"),
        )
        return []
    lo_ids = _can_do_list_lo_ids(event.content)
    if lo_ids is None:
        logger.warning(
            "Can-Do list in %s is malformed; ignored, as the client does",
            getattr(event, "room_id", "?"),
        )
        return []
    return lo_ids


def _score(doc: Mapping[str, Any]) -> Optional[float]:
    score = doc.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    return None


def rank_activities(
    rows: Sequence[Mapping[str, Any]],
    l2: str,
    lo_id: str,
    level: Optional[str] = None,
) -> List[str]:
    """The ranked list an entry with nothing to show pins (RANKED_COUNT has
    the rule and the doc it follows): the Can-Do's listed activities scored
    above LOW_SCORE, best first, then the ones never audited; those scored at
    or below LOW_SCORE only when the Can-Do has nothing else. Between equal
    scores, one at ``level`` (the quest's) comes first; otherwise the rows'
    order holds. ``score`` is the CMS's computed last audit score.
    Visibility and the Can-Do are checked on the rows again."""
    seen: set[str] = set()
    good: List[Tuple[float, bool, str]] = []
    unaudited: List[Tuple[bool, str]] = []
    low: List[Tuple[float, bool, str]] = []
    for doc in rows:
        if not isinstance(doc, Mapping) or not _visible_at(doc, l2):
            continue
        if lo_id not in activity_lo_ids(doc):
            continue
        activity_id = _plan(doc).get("activity_id")
        if not isinstance(activity_id, str) or not activity_id or activity_id in seen:
            continue
        seen.add(activity_id)
        off_level = level is None or _plan(doc).get("cefr_level") != level
        score = _score(doc)
        if score is None:
            unaudited.append((off_level, activity_id))
        elif score > LOW_SCORE:
            good.append((score, off_level, activity_id))
        else:
            low.append((score, off_level, activity_id))
    # Stable sorts: equal keys keep the rows' order.
    good.sort(key=lambda t: (-t[0], t[1]))
    unaudited.sort(key=lambda t: t[0])
    low.sort(key=lambda t: (-t[0], t[1]))
    picked = [a for _, _, a in good] + [a for _, a in unaudited] or [
        a for _, _, a in low
    ]
    return picked[:RANKED_COUNT]


def _first_rows(
    activities: Sequence[Mapping[str, Any]], l2: str
) -> List[Mapping[str, Any]]:
    """The first visible row per activity, as the client dedupes."""
    rows: List[Mapping[str, Any]] = []
    seen: set[str] = set()
    for doc in activities:
        if not isinstance(doc, Mapping) or not _visible_at(doc, l2):
            continue
        activity_id = _plan(doc).get("activity_id")
        if not isinstance(activity_id, str) or not activity_id or activity_id in seen:
            continue
        seen.add(activity_id)
        rows.append(doc)
    return rows


def _listed_by_lo(
    activities: Sequence[Mapping[str, Any]], l2: str
) -> Dict[str, List[str]]:
    by_lo: Dict[str, List[str]] = {}
    for doc in _first_rows(activities, l2):
        for lo_id in activity_lo_ids(doc):
            by_lo.setdefault(lo_id, []).append(_plan(doc)["activity_id"])
    return by_lo


def unshown_selections(
    quest: Mapping[str, Any], activities: Sequence[Mapping[str, Any]]
) -> Tuple[List[str], List[str]]:
    """The selected activity ids the list cannot show, each once, and the
    Can-Dos whose entries have nothing to show: no selection, or none of it
    listed. Those pin the ranked ten."""
    l2 = quest_target_language(quest)
    if l2 is None:
        raise CourseMissionsError(QUEST_MALFORMED, "quest has no target language")
    by_lo = _listed_by_lo(activities, l2)
    missing: Dict[str, None] = {}
    nothing: List[str] = []
    for lo_id, entry in _quest_entries(quest).items():
        selected = _selected(entry)
        listed = set(by_lo.get(lo_id, []))
        gone = [a for a in selected if a not in listed]
        missing.update(dict.fromkeys(gone))
        if len(gone) == len(selected):
            nothing.append(lo_id)
    return list(missing), nothing


def build_course_missions(
    quest: Mapping[str, Any],
    activities: Sequence[Mapping[str, Any]],
    *,
    existing: Optional[Collection[str]] = None,
    unfinished: Sequence[str] = (),
) -> BuiltCourseMissions:
    """The Can-Do list: each learning objective in quest order with its
    activities, and what it could not carry over.

    ``activities`` are ``activities-v2`` rows: the listing's page, the
    selected rows it missed, and the rows read for the ranked ten. Each
    activity is identified by its content id (``res.plan.activity_id``), never
    its row id, and the first row per activity wins, as the client dedupes. An
    entry's selection keeps those of its activities that are listed, in the
    selection's order. An entry with nothing to show, because it selects
    nothing or none of its selection is listed, pins the ranked ten of
    ``activities``. ``existing`` names the selected ids that are rows at all,
    listed or not, so a count can tell hidden from missing; None when unknown.
    ``unfinished`` names the Can-Dos whose ranked rows were not read in time.
    """
    l2 = quest_target_language(quest)
    if l2 is None:
        raise CourseMissionsError(QUEST_MALFORMED, "quest has no target language")
    level = quest_level(quest)
    first_rows = _first_rows(activities, l2)
    by_lo = _listed_by_lo(first_rows, l2)

    missions: List[Dict[str, Any]] = []
    problems: List[Problem] = []
    for lo_id, entry in _quest_entries(quest).items():
        available = set(by_lo.get(lo_id, []))
        selected = _selected(entry)
        chosen = [a for a in selected if a in available]
        gone = [a for a in selected if a not in available]
        if chosen:
            activity_ids = chosen
            if gone:
                counts = _gone_counts(selected, chosen, gone, existing)
                problems.append(Problem(SELECTION_PARTLY_GONE, lo_id, counts))
        else:
            activity_ids = rank_activities(first_rows, l2, lo_id, level)
            if selected:
                counts = _gone_counts(selected, chosen, gone, existing)
                counts["ranked"] = len(activity_ids)
                problems.append(Problem(SELECTION_ALL_GONE, lo_id, counts))
            else:
                problems.append(
                    Problem(SELECTION_UNSET, lo_id, {"ranked": len(activity_ids)})
                )
            if lo_id in unfinished:
                problems.append(
                    Problem(RANKED_UNFINISHED, lo_id, {"ranked": len(activity_ids)})
                )
        mission: Dict[str, Any] = {"lo_id": lo_id, "activity_ids": activity_ids}
        note, note_problems = _note(entry, lo_id)
        problems.extend(note_problems)
        if note is not None:
            mission["note"] = note
        missions.append(mission)
    return BuiltCourseMissions(
        {"version": 1, "revision": 1, "missions": missions},
        problems,
        [lo_id for lo_id in _quest_entries(quest) if lo_id in unfinished],
    )


def _gone_counts(
    selected: Sequence[str],
    chosen: Sequence[str],
    gone: Sequence[str],
    existing: Optional[Collection[str]],
) -> Dict[str, int]:
    """``unavailable``: a row exists but is hidden, private, a translation, in
    another language or filed under another Can-Do. ``not_found``: no row at
    all, the more alarming state. ``unclassified`` when the rows are unknown."""
    counts = {"selected": len(selected), "kept": len(chosen)}
    if existing is None:
        counts["unclassified"] = len(gone)
    else:
        found = set(existing)
        counts["unavailable"] = sum(1 for a in gone if a in found)
        counts["not_found"] = sum(1 for a in gone if a not in found)
    return counts


def build_course_missions_content(
    quest: Mapping[str, Any], activities: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """The list ``build_course_missions`` builds, without its problems."""
    return build_course_missions(quest, activities).content


def _listing_clauses(l2: str) -> List[Tuple[str, str]]:
    return [
        ("where[and][0][req.source_request_hash][exists]", "false"),
        ("where[and][1][hidden][not_equals]", "true"),
        ("where[and][2][visibility][not_equals]", "private"),
        ("where[and][3][res.plan.l2][equals]", l2),
    ]


def selected_query(activity_ids: Sequence[str], l2: str) -> str:
    """The rows of ``activity_ids`` the course listing would list, by content
    id. Which Can-Do each one serves is checked on the rows themselves."""
    params = _listing_clauses(l2)
    params += [
        ("where[and][4][res.plan.activity_id][in]", ",".join(activity_ids)),
        ("limit", str(len(activity_ids) * SELECTED_ROWS_PER_ID)),
        ("depth", "0"),
    ]
    return urlencode(params)


def missed_selections(
    quest: Mapping[str, Any], window: Sequence[Mapping[str, Any]]
) -> List[str]:
    """Every activity the quest's entries select that ``window`` does not hold
    as a visible row, each once, in quest order."""
    l2 = quest_target_language(quest)
    held = {
        _plan(doc).get("activity_id")
        for doc in window
        if isinstance(doc, Mapping) and l2 is not None and _visible_at(doc, l2)
    }
    return list(
        dict.fromkeys(
            a
            for entry in _quest_entries(quest).values()
            for a in _selected(entry)
            if a not in held
        )
    )


def activities_query(lo_ids: Sequence[str], l2: str) -> str:
    """The ``activities-v2`` query the course listing runs (choreo
    list_activity_plans_for_los), without the course-member private rows."""
    params = _listing_clauses(l2)
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


def existing_query(activity_ids: Sequence[str]) -> str:
    """Every row of ``activity_ids``, listed or not: whether a selected
    activity the list cannot show is hidden or gone."""
    return urlencode(
        [
            ("where[res.plan.activity_id][in]", ",".join(activity_ids)),
            ("limit", str(len(activity_ids) * SELECTED_ROWS_PER_ID)),
            ("depth", "0"),
        ]
    )


def ranked_query(lo_id: str, l2: str, tier: str) -> str:
    """One Can-Do's listed activities for its ranked ten, by tier: ``best``
    (scored above LOW_SCORE, best first), ``unaudited`` (no score), ``low``
    (the rest, best first; read only when the first two are empty, so every
    row has a score and Postgres's NULLS FIRST under DESC cannot reorder it)."""
    params = _listing_clauses(l2)
    params.append(("where[and][4][learningObjectiveRefs][contains]", lo_id))
    if tier == "best":
        params += [
            ("where[and][5][score][greater_than]", str(LOW_SCORE)),
            ("sort", "-score"),
        ]
    elif tier == "unaudited":
        params += [("where[and][5][score][exists]", "false"), ("sort", "-updatedAt")]
    else:
        params.append(("sort", "-score"))
    params += [("limit", str(RANKED_COUNT)), ("depth", "0")]
    return urlencode(params)


class CourseMissionsReader:
    def __init__(self, api: ModuleApi, cms_base_url: str, cms_api_key: str):
        self._http = api.http_client
        self._clock = api._hs.get_clock()
        self._base = cms_base_url.rstrip("/") + "/api/"
        self._headers = service_headers(cms_api_key)

    async def _get(self, path: str, *, not_found: Optional[str] = None) -> Any:
        try:
            code, raw = await cms_request(
                self._http,
                "GET",
                self._base + path,
                self._headers,
                max_bytes=MAX_BODY_BYTES,
            )
        except defer.CancelledError:
            raise
        except Exception as e:
            raise CourseMissionsError(CMS_UNAVAILABLE, type(e).__name__) from e
        where = path.split("?")[0]
        if code == 404 and not_found is not None:
            raise CourseMissionsError(not_found, f"CMS {where} answered 404")
        if code >= 500 or code == 429:
            raise CourseMissionsError(CMS_UNAVAILABLE, f"CMS {where} answered {code}")
        if code != 200:
            raise CourseMissionsError(CMS_REFUSED, f"CMS {where} answered {code}")
        try:
            return json.loads(raw)
        except ValueError as e:
            raise CourseMissionsError(CMS_MALFORMED, f"CMS {where}: not JSON") from e

    async def _docs(self, path: str, what: str) -> List[Mapping[str, Any]]:
        found = await self._get(path)
        docs = found.get("docs") if isinstance(found, Mapping) else None
        if not isinstance(docs, list):
            raise CourseMissionsError(
                CMS_MALFORMED, f"{what} read returned no docs list"
            )
        return docs

    async def _quest(self, quest_id: str) -> Mapping[str, Any]:
        quest = await self._get(
            f"quest-plans/{quote(quest_id, safe='')}?depth=0",
            not_found=QUEST_NOT_FOUND,
        )
        if not isinstance(quest, Mapping):
            raise CourseMissionsError(CMS_MALFORMED, "quest read returned no document")
        return quest

    async def quest_lo_ids(self, quest_id: str) -> List[str]:
        """The quest's Missions, in order; raises when the CMS cannot say."""
        return quest_lo_ids(await self._quest(quest_id))

    async def quest_selections(self, quest_id: str) -> Dict[str, List[str]]:
        """Each quest entry's selected activity ids, by Can-Do; raises when
        the CMS cannot say."""
        return {
            lo_id: _selected(entry)
            for lo_id, entry in _quest_entries(await self._quest(quest_id)).items()
        }

    async def quest_language(self, quest_id: str) -> Optional[str]:
        """The quest's target language, the one its courses list at."""
        return quest_target_language(await self._quest(quest_id))

    async def listable_ids(self, activity_ids: Sequence[str], l2: str) -> Set[str]:
        """Which of these activity content ids the course listing lists at
        ``l2`` (visible, canonical, not private), read by id as the builder
        reads selections. Raises when the CMS cannot say."""
        listed: Set[str] = set()
        ids = list(dict.fromkeys(activity_ids))
        for start in range(0, len(ids), SELECTED_CHUNK):
            for doc in await self._selected_rows(
                ids[start : start + SELECTED_CHUNK], l2
            ):
                activity_id = _plan(doc).get("activity_id")
                if isinstance(activity_id, str) and _visible_at(doc, l2):
                    listed.add(activity_id)
        return listed

    async def _build(self, quest_id: str, deadline: float) -> BuiltCourseMissions:
        quest = await self._quest(quest_id)
        l2 = quest_target_language(quest)
        lo_ids = quest_lo_ids(quest)
        if l2 is None:
            raise CourseMissionsError(QUEST_MALFORMED, "quest has no target language")
        if not lo_ids:
            return build_course_missions(quest, [])
        found = await self._get("activities-v2?" + activities_query(lo_ids, l2))
        docs = found.get("docs") if isinstance(found, Mapping) else None
        if not isinstance(docs, list):
            raise CourseMissionsError(
                CMS_MALFORMED, "activities read returned no docs list"
            )
        total = found.get("totalDocs")
        if isinstance(total, int) and total > len(docs):
            logger.info(
                "Can-Do list for quest %s: the library page holds the newest %d "
                "of %d rows; selected activities outside it are read by id",
                quest_id,
                len(docs),
                total,
            )
        missed = missed_selections(quest, docs)
        # Concurrently: the whole read is bounded by FETCH_TIMEOUT_SECONDS.
        extra = await yieldable_gather_results(self._selected_rows, _chunks(missed), l2)
        rows = [*docs, *(row for chunk in extra for row in chunk)]
        unshown, nothing = unshown_selections(quest, rows)
        # Only for what the page and the selections leave unanswered: whether
        # a selected activity that cannot be shown is hidden or gone, and the
        # best-scored rows of each Can-Do with nothing to show. Started
        # together and both stopped at ``deadline``; ``_existing`` never
        # raises, so a failed ranked read is the only failure to wait for, and
        # it fails the whole read.
        existing_read = run_in_background(self._existing, unshown, deadline)
        ranked_read = run_in_background(self._ranked_rows, nothing, l2, deadline)
        existing = await make_deferred_yieldable(existing_read)
        ranked = await make_deferred_yieldable(ranked_read)
        unfinished = [lo_id for lo_id in nothing if lo_id not in ranked]
        return build_course_missions(
            quest,
            [*rows, *(row for found in ranked.values() for row in found)],
            existing=existing if unshown else (),
            unfinished=unfinished,
        )

    async def _selected_rows(
        self, chunk: Sequence[str], l2: str
    ) -> List[Mapping[str, Any]]:
        """The listed rows of one chunk of selected ids. A failure raises, as
        the page read does: a course never starts with part of a selection."""
        found = await self._get("activities-v2?" + selected_query(chunk, l2))
        docs = found.get("docs") if isinstance(found, Mapping) else None
        if not isinstance(docs, list):
            raise CourseMissionsError(
                CMS_MALFORMED, "selected activities read returned no docs list"
            )
        total = found.get("totalDocs")
        if isinstance(total, int) and total > len(docs):
            raise CourseMissionsError(
                CMS_MALFORMED,
                f"selected activities read returned {len(docs)} of {total} rows",
            )
        return docs

    async def _existing(
        self, activity_ids: Sequence[str], deadline: float
    ) -> Optional[List[str]]:
        """Which of ``activity_ids`` are rows at all, or None when that cannot
        be read by ``deadline``: it only labels a capture, so it never fails
        or delays the list."""
        if not activity_ids:
            return []
        left = deadline - self._clock.time()
        if left <= 0:
            return None

        async def read_all() -> List[List[str]]:
            return await yieldable_gather_results(
                self._existing_chunk, _chunks(activity_ids)
            )

        try:
            chunks = await make_deferred_yieldable(
                timeout_deferred(
                    deferred=run_in_background(read_all),
                    timeout=left,
                    clock=self._clock,
                )
            )
        # silent-ok: a label on the Sentry capture, not the list; logged, and the capture counts the ids as unclassified
        except (CourseMissionsError, defer.TimeoutError) as e:
            logger.warning(
                "Can-Do list: could not read whether %d selected activities "
                "exist: %s",
                len(activity_ids),
                e,
            )
            return None
        return [a for chunk in chunks for a in chunk]

    async def _existing_chunk(self, chunk: Sequence[str]) -> List[str]:
        docs = await self._docs(
            "activities-v2?" + existing_query(chunk), "existing activities"
        )
        return [
            a
            for a in (
                _plan(d).get("activity_id") for d in docs if isinstance(d, Mapping)
            )
            if isinstance(a, str)
        ]

    async def _ranked_rows(
        self, lo_ids: Sequence[str], l2: str, deadline: float
    ) -> Dict[str, List[Mapping[str, Any]]]:
        """Each Can-Do's ranked rows, read RANKED_CONCURRENCY Can-Dos at a time
        until ``deadline``. A Can-Do not read by then is left out, so the list
        is built from what finished; a failed read raises."""
        found: Dict[str, List[Mapping[str, Any]]] = {}

        async def read_one(lo_id: str) -> None:
            # No new read once time is up; the ones in flight are cut below.
            if self._clock.time() < deadline:
                found[lo_id] = await self._ranked_one(lo_id, l2)

        left = deadline - self._clock.time()
        if not lo_ids or left <= 0:
            return found
        try:
            await make_deferred_yieldable(
                timeout_deferred(
                    deferred=run_in_background(
                        concurrently_execute, read_one, lo_ids, RANKED_CONCURRENCY
                    ),
                    timeout=left,
                    clock=self._clock,
                )
            )
        # silent-ok: running out of time is reported with the list, as ranked_unfinished per Can-Do, and the caller builds again
        except defer.TimeoutError:
            logger.warning(
                "Can-Do list: %d of %d Can-Dos' ranked rows not read in time",
                len(lo_ids) - len(found),
                len(lo_ids),
            )
        return found

    async def _ranked_one(self, lo_id: str, l2: str) -> List[Mapping[str, Any]]:
        """The rows one Can-Do's ranked ten is picked from: the best-scored
        and the unaudited read together, the low-scored only when both are
        empty. Two tiers, not one read sorted by score, because Postgres
        sorts unscored rows first under a descending sort and would fill the
        page with them."""
        best, unaudited = await yieldable_gather_results(
            lambda tier: self._docs(
                "activities-v2?" + ranked_query(lo_id, l2, tier), "ranked activities"
            ),
            ["best", "unaudited"],
        )
        if not rank_activities([*best, *unaudited], l2, lo_id):
            return await self._docs(
                "activities-v2?" + ranked_query(lo_id, l2, "low"), "ranked activities"
            )
        return [*best, *unaudited]

    async def _attempt(self, quest_id: str, timeout: float) -> BuiltCourseMissions:
        deadline = self._clock.time() + timeout - RANKED_MARGIN_SECONDS
        try:
            return await make_deferred_yieldable(
                timeout_deferred(
                    deferred=run_in_background(self._build, quest_id, deadline),
                    timeout=timeout,
                    clock=self._clock,
                )
            )
        except defer.TimeoutError as e:
            raise CourseMissionsError(
                TIMEOUT, f"no answer within {timeout:.1f}s"
            ) from e

    async def read(
        self,
        quest_id: str,
        *,
        budget: float = FETCH_TIMEOUT_SECONDS,
        report: bool = True,
    ) -> BuiltCourseMissions:
        """The Can-Do list for ``quest_id``, with what it could not carry over
        (captured here unless ``report`` is off). Raises
        ``CourseMissionsError`` when none can be built; the caller captures
        that, since only it knows what the course does without a list.

        A failure the next attempt may get past is tried once more while
        enough of ``budget`` is left, so the whole read stays within it: a
        caller that times out and retries a creation would make a second
        course. A background caller, which nobody waits on, may pass more."""
        if not quest_id:
            raise CourseMissionsError(NO_QUEST, "the request names no quest")
        started = self._clock.time()
        try:
            built = await self._attempt(quest_id, budget)
        except CourseMissionsError as e:
            left = budget - (self._clock.time() - started)
            if e.code not in RETRYABLE or left < MIN_RETRY_SECONDS:
                raise
            logger.warning(
                "Can-Do list for quest %s: %s; trying once more", quest_id, e
            )
            built = await self._attempt(quest_id, left)
        if report:
            report_problems(quest_id, built.problems)
        return built


def _chunks(ids: Sequence[str]) -> List[Sequence[str]]:
    return [
        ids[start : start + SELECTED_CHUNK]
        for start in range(0, len(ids), SELECTED_CHUNK)
    ]


def report_problems(quest_id: str, problems: Sequence[Problem]) -> None:
    """One log line and one Sentry event per kind of problem in a build,
    carrying the quest, each entry's Can-Do and its counts."""
    by_code: Dict[str, List[Problem]] = {}
    for problem in problems:
        by_code.setdefault(problem.code, []).append(problem)
    for code, found in by_code.items():
        level = PROBLEM_LEVELS[code]
        entries = [{"lo_id": p.lo_id, **p.counts} for p in found]
        logger.log(
            logging.ERROR if level == "error" else logging.WARNING,
            "Can-Do list for quest %s: %s in %d entries: %s",
            quest_id,
            code,
            len(found),
            entries,
        )
        capture_message(
            f"Can-Do list: {code}",
            level=level,
            tags={"course_missions.problem": code},
            context={"quest_id": quest_id, "count": len(found), "entries": entries},
        )


def capture_read_failure(error: CourseMissionsError, quest_id: str, **ids: str) -> None:
    """The caller's capture of a list that could not be built: its code, the
    quest and the caller's own ids (a room, an invitation). Logged too, so a
    server without Sentry still says which courses need their list built."""
    logger.error(
        "Can-Do list not built for quest %s (%s): %s",
        quest_id,
        ", ".join(f"{k}={v}" for k, v in ids.items()),
        error,
    )
    capture_message(
        f"Can-Do list not built: {error.code}",
        level="error",
        tags={"course_missions.failure": error.code},
        context={"quest_id": quest_id, "detail": error.detail, **ids},
    )
