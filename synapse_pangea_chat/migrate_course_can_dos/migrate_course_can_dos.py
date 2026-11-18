"""Server-admin endpoint: move one course space from old (pre-merge) Can-Do ids
to the merged Can-Dos that replace them, keeping every teacher edit.

``POST /_synapse/client/pangea/v1/migrate_course_can_dos``

One-off, for the Can-Do merge (pangeachat/.github#832); the operator runs it
through ``scripts/migrate_course_can_dos.py`` by the runbook skill
(2-step-choreographer ``migrate-plans-to-merged-can-dos``). Removed with the
migration, after its last progress pass.

Contract. Server admins only, one space per call. Input: the room id; the
environment's map of old Can-Do row id to merged row id (a merged id is never
itself mapped); optionally ``valid_ids``, every Can-Do id on the environment,
refused unless it holds every merged id, so a wrong file can never empty a
course; and either ``dry_run: true`` or ``expected``, the event ids of the list
and pins the dry run read. The answer gives what changes as ids, counts and
lengths. Refused (409): no joined local instructor; a space changed since its
dry run; a write the room refuses.

What it does:

- The Can-Do list (``pangea.course_missions``) is rewritten as raw JSON, so
  fields this module does not model, Content Studio's included, survive. Each
  old id in ``missions`` and ``removed_missions`` becomes its merged id; old
  Can-Dos landing on one merged Can-Do combine (Gabby, 2026-11-11), by the
  rules below. ``revision`` is bumped, so Studio refuses a publish from a draft
  opened before the move.
- With ``valid_ids``, an entry or pin naming neither an old id the map moves
  nor a Can-Do on the environment is removed (Will, 2026-11-15); the app
  already showed nothing for it.
- The pins in ``pangea.teacher_mode`` are re-keyed the same way, every other
  field kept, whoever sent them, since the app reads them from any sender.
- A list readers ignore is left alone and reported with the old ids it still
  names: one failing the client's strict parse (``copy_problem``, field for
  field, including its ``due_at`` rule, ``due_at_readable``), or one whose
  sender is below power level 100. A rewrite therefore never makes an ignored
  list take effect. Pins that are not an object are left and reported.
- Both are sent as the space's strongest local instructor, list first, as
  ``set_course_plan`` does, only while the space still holds both events the
  dry run read; an edited space is skipped and reported. The check and the
  send are not atomic.

Combining a live course's entries (the one written authority for it; the
dashboard does not combine a live course's drafts itself):

- Place: the first occurrence's. Occurrences directly after it form one span;
  a later occurrence with entries between gives up its slot.
- Due date: the span's last date, or the latest before it when the last has
  none. Earlier dates in the span, and every given-up slot's date, are dropped.
- Activities: the first entry's order, then the others' new ids; removed and
  deleted ids pool the same way. One entry's shown activity stays shown.
- Note: distinct notes joined with " / "; over 280 UTF-16 units, the first note
  with "(+N more)", cut to fit. An entry left with no note gets its old
  Can-Dos' statements by the quests' rule (Will, 2026-11-13); one with a note,
  or combined with one, gains none.
- City and other fields: from the first entry that has them; the others are
  dropped.
- Removed Can-Dos combine the same way. When the course still shows the merged
  Can-Do, a removed old one's activities join it as hidden, and it can no
  longer be restored on its own.
- Pins pool. An old Can-Do listed with no pins adds nothing, so its activities
  stop showing under the pinned merged Can-Do. A pin value that is not a list
  of ids is dropped.

Every drop above is counted in the answer, for the operator to read before the
apply. Answers, logs and Sentry carry ids, counts, lengths and error types,
never a teacher's words (``report.py``); a trimmed note's text stays in the
room's history.
"""

from __future__ import annotations

import logging
import re
from typing import (
    AbstractSet,
    Any,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from synapse.api.constants import EventTypes
from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from synapse.util.frozenutils import unfreeze
from twisted.web.resource import Resource

from synapse_pangea_chat.email_invite.course_missions import (
    COURSE_MISSIONS_STATE_EVENT_TYPE,
)
from synapse_pangea_chat.migrate_course_can_dos.report import report, report_failure
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.instructor_access import (
    joined_local_instructors,
    user_power,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.migrate_course_can_dos.migrate_course_can_dos"
)

TEACHER_MODE_STATE_EVENT_TYPE = "pangea.teacher_mode"
PINS_KEY = "pinned_activities_by_objective"

COPY_VERSION = 1
# Readers ignore a copy whose sender is below this level now.
INSTRUCTOR_POWER = 100
# The app's maxNoteLength, counted as Dart counts a String: UTF-16 units.
MAX_NOTE_UTF16 = 280
# The app shows a note inline in two or three lines, so no line break.
NOTE_SEPARATOR = " / "
MORE_NOTES = " (+{} more)"
ELLIPSIS = "…"

MAX_ID_LENGTH = 255
MAX_MAP_ENTRIES = 20_000
# A Can-Do statement is one sentence; this bounds a malformed input.
MAX_STATEMENT_LENGTH = 2000

COPY = "course_missions"
PINS = "teacher_mode"


# --------------------------------------------------------------------------
# Pure decisions
# --------------------------------------------------------------------------


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _cut(text: str, budget: int) -> str:
    """The longest start of ``text`` within ``budget`` UTF-16 units, never
    splitting a character."""
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > budget:
            return text[:index]
    return text


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_id_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


# The client's due-date rule (CourseMissionsEvent.tryParse): an instant, so a
# "T" and then an offset or "Z" at the very end, that Dart's DateTime.tryParse
# reads. Dart's grammar is ported as written; it rolls out-of-range fields over
# (a 30 February is 2 March) and refuses an instant past +-8.64e15 ms.
_HAS_UTC_OFFSET = re.compile(r"T.*(Z|[+-]\d{2}:?\d{2})\Z", re.ASCII)
_DART_DATE_TIME = re.compile(
    r"([+-]?\d{4,6})-?(\d\d)-?(\d\d)"
    r"(?:[ T](\d\d)(?::?(\d\d)(?::?(\d\d)(?:[.,](\d+))?)?)?"
    r"( ?[zZ]| ?([-+])(\d\d)(?::?(\d\d))?)?)?\Z",
    re.ASCII,
)
_DART_MAX_MS = 8_640_000_000_000_000


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days from 1970-01-01, proleptic Gregorian, any year."""
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def due_at_readable(value: Any) -> bool:
    """Whether the app reads ``value`` as a due date."""
    if not isinstance(value, str) or not _HAS_UTC_OFFSET.search(value):
        return False
    match = _DART_DATE_TIME.match(value)
    if match is None:
        return False
    year, month, day, hour, minute, second, fraction = (
        match.group(i) for i in range(1, 8)
    )
    month_index = int(month) - 1
    days = _days_from_civil(int(year) + month_index // 12, month_index % 12 + 1, 1)
    days += int(day) - 1
    ms = (
        ((days * 24 + int(hour or 0)) * 60 + int(minute or 0)) * 60 + int(second or 0)
    ) * 1000 + int((fraction or "0")[:3].ljust(3, "0"))
    if match.group(9):
        offset = int(match.group(10)) * 60 + int(match.group(11) or 0)
        ms -= (1 if match.group(9) == "+" else -1) * offset * 60_000
    return abs(ms) <= _DART_MAX_MS


def _pool(lists: Iterable[Any]) -> List[str]:
    """Every id once, in the order first seen: the first list's order, then
    each later list's new ids."""
    return list(
        dict.fromkeys(
            item
            for value in lists
            if isinstance(value, list)
            for item in value
            if isinstance(item, str)
        )
    )


def copy_problem(content: Any) -> Optional[str]:
    """Why the app would refuse this Can-Do list, or None when it reads it.

    The checks of the client's strict parse (``CourseMissionsEvent.tryParse``)
    on every field a rewrite touches, plus the dashboard's type of
    ``deleted_activity_ids``. A refused copy is never rewritten: sent again by
    an instructor, a combined entry could repair it, and a copy readers
    ignored would start to apply.
    """
    if not isinstance(content, Mapping):
        return "not an object"
    version: Any = content.get("version")
    if not _is_int(version) or version != COPY_VERSION:
        return "unknown version"
    revision: Any = content.get("revision")
    if not _is_int(revision) or revision < 1:
        return "no revision"
    time_zone = content.get("time_zone")
    if time_zone is not None and (not isinstance(time_zone, str) or not time_zone):
        return "time_zone is not a zone name"
    missions = content.get("missions")
    if not isinstance(missions, list):
        return "no missions list"
    seen = set()
    for entry in missions:
        if not isinstance(entry, Mapping):
            return "a mission is not an object"
        lo_id = entry.get("lo_id")
        if not isinstance(lo_id, str) or not lo_id:
            return "a mission has no lo_id"
        if lo_id in seen:
            return f"{lo_id} is listed twice"
        seen.add(lo_id)
        if not _is_id_list(entry.get("activity_ids")):
            return f"{lo_id}: activity_ids is not a list of ids"
        for key in ("removed_activity_ids", "deleted_activity_ids"):
            if entry.get(key) is not None and not _is_id_list(entry[key]):
                return f"{lo_id}: {key} is not a list of ids"
        note = entry.get("note")
        if note is not None and (
            not isinstance(note, str) or utf16_len(note) > MAX_NOTE_UTF16
        ):
            return f"{lo_id}: note is not text of at most {MAX_NOTE_UTF16}"
        if entry.get("due_at") is not None and not due_at_readable(entry["due_at"]):
            return f"{lo_id}: due_at is not an instant the app reads"
    removed = content.get("removed_missions")
    if removed is not None:
        if not isinstance(removed, list):
            return "removed_missions is not a list"
        for entry in removed:
            if not isinstance(entry, Mapping):
                return "a removed mission is not an object"
            lo_id = entry.get("lo_id")
            ids: Any = entry.get("activity_ids")
            shown: Any = entry.get("shown_count")
            if (
                not isinstance(lo_id, str)
                or not lo_id
                or not _is_id_list(ids)
                or not _is_int(shown)
                or not 0 <= shown <= len(ids)
            ):
                return "a removed mission is malformed"
    return None


def join_notes(notes: Sequence[Any]) -> Tuple[Optional[str], List[str]]:
    """The combined entry's note, and the text it drops.

    Each distinct non-blank note once, in entry order, joined. Over the limit,
    the first note is kept with "(+N more)", cut to fit when it is long
    itself."""
    texts: Dict[str, str] = {}
    for note in notes:
        if isinstance(note, str) and note.strip():
            texts.setdefault(note.strip(), note)
    if not texts:
        return None, []
    if len(texts) == 1:
        return next(iter(texts.values())), []
    parts = list(texts)
    joined = NOTE_SEPARATOR.join(parts)
    if utf16_len(joined) <= MAX_NOTE_UTF16:
        return joined, []
    suffix = MORE_NOTES.format(len(parts) - 1)
    budget = MAX_NOTE_UTF16 - utf16_len(suffix)
    first = parts[0]
    if utf16_len(first) <= budget:
        return first + suffix, parts[1:]
    kept = _cut(first, budget - utf16_len(ELLIPSIS))
    return kept + ELLIPSIS + suffix, [first[len(kept) :], *parts[1:]]


def _same_statement(a: str, b: str) -> bool:
    """The quest step's test (choreo migrate_to_merged_can_dos): case, outer
    space and a final full stop do not count."""
    return a.strip().rstrip(".").casefold() == b.strip().rstrip(".").casefold()


def _has_note(entry: Mapping[str, Any]) -> bool:
    note = entry.get("note")
    return isinstance(note, str) and bool(note.strip())


class OldWording(NamedTuple):
    # The old Can-Dos whose statement makes the note, in entry order.
    from_ids: List[str]
    texts: List[str]
    # Old Can-Dos this course lists that have no statement to write.
    no_statement: List[str]


def old_wording(
    lo_id: str,
    sources: Sequence[Mapping[str, Any]],
    can_do_map: Mapping[str, str],
    statements: Mapping[str, str],
) -> OldWording:
    """The old Can-Dos' wording for an entry that lands on ``lo_id`` with no
    note, by the rule the quests follow: each moved entry's statement in entry
    order, leaving out one that only repeats the merged Can-Do's own. Empty
    when any of ``sources`` already has a note: a teacher's note is never
    added to."""
    if any(_has_note(source) for source in sources):
        return OldWording([], [], [])
    merged = statements.get(lo_id)
    out = OldWording([], [], [])
    for source in sources:
        old_id = source["lo_id"]
        if old_id not in can_do_map:
            continue
        text = statements.get(old_id)
        if text is None or not text.strip():
            out.no_statement.append(old_id)
            continue
        if merged is not None and _same_statement(text, merged):
            continue
        out.from_ids.append(old_id)
        out.texts.append(text.strip())
    return out


def _add_old_wording(
    entry: Dict[str, Any],
    sources: Sequence[Mapping[str, Any]],
    can_do_map: Mapping[str, str],
    statements: Mapping[str, str],
    report: Dict[str, Any],
) -> None:
    """Gives ``entry`` a note from the old wording where it has none."""
    wording = old_wording(entry["lo_id"], sources, can_do_map, statements)
    report["no_statement"].extend(wording.no_statement)
    note, dropped = join_notes(wording.texts)
    if note is None:
        return
    entry["note"] = note
    report["notes_added"].append(
        {"lo_id": entry["lo_id"], "note": note, "from": wording.from_ids}
    )
    if dropped:
        report["notes_trimmed"].append(_trimmed(entry["lo_id"], note, dropped))


def _trimmed(lo_id: str, note: Optional[str], dropped: Sequence[str]) -> Dict[str, Any]:
    """A trimmed note as lengths and counts: the report leaves the server, and
    the text stays in the room's history, in the event the dry run read."""
    return {
        "lo_id": lo_id,
        "kept_length": utf16_len(note or ""),
        "dropped_count": len(dropped),
        "dropped_length": sum(utf16_len(text) for text in dropped),
    }


_MODELLED = frozenset(
    (
        "lo_id",
        "activity_ids",
        "removed_activity_ids",
        "deleted_activity_ids",
        "note",
        "due_at",
        "city",
    )
)


def _distinct_others(values: Iterable[Any], kept: Any) -> int:
    """How many distinct set values differ from the one kept."""
    seen: List[Any] = []
    for value in values:
        if value is not None and value != kept and value not in seen:
            seen.append(value)
    return len(seen)


def _combine(
    lo_id: str, sources: Sequence[Dict[str, Any]], span: Sequence[Dict[str, Any]]
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    """One entry for the old entries ``sources`` (in list order) that map to
    ``lo_id``; ``span`` is the run of them next to the first. Fields this
    module does not model come from the first. Returns the note text dropped
    and the counts of what else the teacher set that the entry leaves out.
    """
    entry = dict(sources[0])
    entry["lo_id"] = lo_id
    shown = _pool(source.get("activity_ids") for source in sources)
    entry["activity_ids"] = shown
    # An activity one Can-Do shows and another hid stays shown, as readers
    # already show an id in both lists.
    shown_set = set(shown)
    removed = [
        a
        for a in _pool(source.get("removed_activity_ids") for source in sources)
        if a not in shown_set
    ]
    removed_set = set(removed)
    deleted = [
        a
        for a in _pool(source.get("deleted_activity_ids") for source in sources)
        if a in removed_set
    ]
    note, dropped = join_notes([source.get("note") for source in sources])
    # The span ends where its last entry ended; one with no date leaves the
    # latest date before it, rather than clearing a date the teacher set.
    due_at = next(
        (s["due_at"] for s in reversed(span) if s.get("due_at") is not None), None
    )
    city = next((s["city"] for s in sources if s.get("city") is not None), None)
    for key, value in (
        ("removed_activity_ids", removed),
        ("deleted_activity_ids", deleted),
        ("note", note),
        ("due_at", due_at),
        ("city", city),
    ):
        if value:
            entry[key] = value
        else:
            entry.pop(key, None)
    hid = set(_pool(source.get("removed_activity_ids") for source in sources))
    drops = {
        "due_dates_dropped": _distinct_others(
            (source.get("due_at") for source in sources), due_at
        ),
        "cities_dropped": _distinct_others(
            (source.get("city") for source in sources), city
        ),
        "fields_dropped": sorted(
            {
                key
                for source in sources[1:]
                for key, value in source.items()
                if key not in _MODELLED and entry.get(key, _MISSING) != value
            }
        ),
        "hidden_now_shown": len(hid & shown_set),
    }
    return entry, dropped, drops


_MISSING = object()


def _rewrite_missions(
    missions: List[Dict[str, Any]],
    can_do_map: Mapping[str, str],
    statements: Mapping[str, str],
    report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    new_ids = [can_do_map.get(entry["lo_id"], entry["lo_id"]) for entry in missions]
    places: Dict[str, List[int]] = {}
    for place, new_id in enumerate(new_ids):
        places.setdefault(new_id, []).append(place)
    out: List[Dict[str, Any]] = []
    for place, entry in enumerate(missions):
        new_id = new_ids[place]
        group = places[new_id]
        if group[0] != place:
            continue  # combined into its first occurrence
        if len(group) == 1:
            moved = {**entry, "lo_id": new_id}
            _add_old_wording(moved, [entry], can_do_map, statements, report)
            out.append(moved)
            continue
        span = [group[0]]
        for later in group[1:]:
            if later != span[-1] + 1:
                break
            span.append(later)
        combined, dropped, drops = _combine(
            new_id, [missions[p] for p in group], [missions[p] for p in span]
        )
        sources = [missions[p] for p in group]
        _add_old_wording(combined, sources, can_do_map, statements, report)
        out.append(combined)
        report["combined"].append(
            {
                "lo_id": new_id,
                "from": [missions[p]["lo_id"] for p in group],
                "slots_given_up": [missions[p]["lo_id"] for p in group[len(span) :]],
                **drops,
            }
        )
        if dropped:
            report["notes_trimmed"].append(
                _trimmed(new_id, combined.get("note"), dropped)
            )
    return out


def _rewrite_removed(
    removed: List[Dict[str, Any]],
    can_do_map: Mapping[str, str],
    shown_by_id: Mapping[str, Dict[str, Any]],
    report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for entry in removed:
        groups.setdefault(can_do_map.get(entry["lo_id"], entry["lo_id"]), []).append(
            entry
        )
    out: List[Dict[str, Any]] = []
    for new_id, sources in groups.items():
        shown_entry = shown_by_id.get(new_id)
        if shown_entry is not None:
            # The course still shows this Can-Do: what was removed stays
            # credited there, hidden, so the Can-Do is never listed twice.
            shown = set(shown_entry["activity_ids"])
            hidden = [
                a
                for a in _pool(
                    [
                        shown_entry.get("removed_activity_ids"),
                        *(source["activity_ids"] for source in sources),
                    ]
                )
                if a not in shown
            ]
            before = set(shown_entry.get("removed_activity_ids") or [])
            if hidden:
                shown_entry["removed_activity_ids"] = hidden
            report["removed_into_missions"].append(
                {
                    "lo_id": new_id,
                    "from": [source["lo_id"] for source in sources],
                    # Hidden there now, no longer a removed Can-Do to restore.
                    "activities_hidden": len(set(hidden) - before),
                }
            )
            continue
        if len(sources) == 1:
            out.append({**sources[0], "lo_id": new_id})
            continue
        # What each showed at removal, then what each hid, so a restore puts
        # back what was on show.
        was_shown = _pool(s["activity_ids"][: s["shown_count"]] for s in sources)
        shown_before = set(was_shown)
        was_hidden = [
            a
            for a in _pool(s["activity_ids"][s["shown_count"] :] for s in sources)
            if a not in shown_before
        ]
        out.append(
            {
                **sources[0],
                "lo_id": new_id,
                "activity_ids": was_shown + was_hidden,
                "shown_count": len(was_shown),
            }
        )
        report["removed_combined"].append(
            {"lo_id": new_id, "from": [source["lo_id"] for source in sources]}
        )
    return out


def _old_ids_in_copy(content: Any, can_do_map: Mapping[str, str]) -> int:
    """Old ids a copy left as it is still names."""
    if not isinstance(content, Mapping):
        return 0
    return sum(
        1
        for key in ("missions", "removed_missions")
        if isinstance(content.get(key), list)
        for entry in content[key]
        if isinstance(entry, Mapping)
        and isinstance(entry.get("lo_id"), str)
        and entry["lo_id"] in can_do_map
    )


class Rewrite(NamedTuple):
    # The content to send, or None when nothing changes.
    content: Optional[Dict[str, Any]]
    report: Dict[str, Any]


def _unknown(
    lo_id: str, can_do_map: Mapping[str, str], valid_ids: Optional[AbstractSet[str]]
) -> bool:
    """An id that is no Can-Do on the environment: neither an old id the map
    moves nor one of the environment's Can-Dos. Never, without valid ids."""
    return valid_ids is not None and lo_id not in can_do_map and lo_id not in valid_ids


def _drop_unknown(
    entries: List[Dict[str, Any]],
    where: str,
    can_do_map: Mapping[str, str],
    valid_ids: Optional[AbstractSet[str]],
    report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    kept = []
    for entry in entries:
        if not _unknown(entry["lo_id"], can_do_map, valid_ids):
            kept.append(entry)
            continue
        # Ids and counts only: what the teacher had set on it, never its text.
        report["unknown_removed"].append(
            {
                "lo_id": entry["lo_id"],
                "list": where,
                "activities": len(entry.get("activity_ids") or []),
                "note": _has_note(entry),
                "due_at": entry.get("due_at") is not None,
            }
        )
    return kept


def rewrite_copy(
    content: Mapping[str, Any],
    can_do_map: Mapping[str, str],
    statements: Optional[Mapping[str, str]] = None,
    valid_ids: Optional[AbstractSet[str]] = None,
) -> Rewrite:
    """The copy with every old Can-Do id replaced, entries that now name the
    same Can-Do combined, at one revision above. An entry left with no note
    gets the old Can-Dos' wording from ``statements`` (Can-Do row id to its
    statement, old and merged). With ``valid_ids`` (every Can-Do id on the
    environment), an entry naming no Can-Do there is removed first.
    ``content`` must pass ``copy_problem``."""
    copy: Dict[str, Any] = unfreeze(content)
    report: Dict[str, Any] = {"unknown_removed": []}
    missions: List[Dict[str, Any]] = _drop_unknown(
        copy["missions"], "missions", can_do_map, valid_ids, report
    )
    removed: List[Dict[str, Any]] = _drop_unknown(
        copy.get("removed_missions") or [],
        "removed_missions",
        can_do_map,
        valid_ids,
        report,
    )
    report.update(
        {
            "ids_changed": sum(
                1 for entry in (*missions, *removed) if entry["lo_id"] in can_do_map
            ),
            "combined": [],
            "removed_combined": [],
            "removed_into_missions": [],
            "notes_trimmed": [],
            "notes_added": [],
            "no_statement": [],
        }
    )
    if not report["ids_changed"] and not report["unknown_removed"]:
        return Rewrite(None, report)
    new_missions = _rewrite_missions(missions, can_do_map, statements or {}, report)
    new_removed = _rewrite_removed(
        removed,
        can_do_map,
        {entry["lo_id"]: entry for entry in new_missions},
        report,
    )
    copy["missions"] = new_missions
    if new_removed:
        copy["removed_missions"] = new_removed
    else:
        copy.pop("removed_missions", None)
    copy["revision"] = copy["revision"] + 1
    return Rewrite(copy, report)


def rewrite_pins(
    content: Mapping[str, Any],
    can_do_map: Mapping[str, str],
    listed: Sequence[str] = (),
    valid_ids: Optional[AbstractSet[str]] = None,
) -> Rewrite:
    """``pangea.teacher_mode`` with its pins re-keyed to the merged Can-Dos;
    pins that land on one Can-Do pool. Every other field is kept. ``listed``
    is the old Can-Do ids the course's list names, to count the old Can-Dos
    that had no pins and so show nothing of their own under a pinned merged
    Can-Do."""
    pins = content.get(PINS_KEY)
    report: Dict[str, Any] = {
        "keys_changed": 0,
        "pooled": [],
        "unpinned_siblings": [],
        "pin_values_dropped": 0,
        "unknown_removed": [],
    }
    if not isinstance(pins, Mapping):
        return Rewrite(None, report)
    report["unknown_removed"] = [
        key for key in pins if _unknown(key, can_do_map, valid_ids)
    ]
    if report["unknown_removed"]:
        pins = {
            key: value
            for key, value in pins.items()
            if key not in report["unknown_removed"]
        }
    report["keys_changed"] = sum(1 for key in pins if key in can_do_map)
    if not report["keys_changed"] and not report["unknown_removed"]:
        return Rewrite(None, report)
    sources: Dict[str, List[str]] = {}
    for key in pins:
        sources.setdefault(can_do_map.get(key, key), []).append(key)
    new_pins: Dict[str, Any] = {}
    for new_key, keys in sources.items():
        if len(keys) == 1:
            new_pins[new_key] = unfreeze(pins[keys[0]])
        else:
            new_pins[new_key] = _pool(pins[key] for key in keys)
            report["pooled"].append({"lo_id": new_key, "from": keys})
            report["pin_values_dropped"] += sum(
                1 for key in keys if not _is_id_list(pins[key])
            )
        if any(key in can_do_map for key in keys):
            unpinned = [
                old
                for old in dict.fromkeys(listed)
                if can_do_map.get(old) == new_key and old not in pins
            ]
            if unpinned:
                report["unpinned_siblings"].append({"lo_id": new_key, "from": unpinned})
    mode: Dict[str, Any] = unfreeze(content)
    mode[PINS_KEY] = new_pins
    return Rewrite(mode, report)


def parse_can_do_map(value: Any) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """``(map, None)`` or ``(None, error)``. The map is one hop: a merged id
    is never itself mapped."""
    if not isinstance(value, dict) or not value:
        return None, "'can_do_map' must be an object of old Can-Do id to merged id"
    if len(value) > MAX_MAP_ENTRIES:
        return None, f"'can_do_map' holds more than {MAX_MAP_ENTRIES} ids"
    for old, new in value.items():
        if _valid_id(old) is None or _valid_id(new) is None:
            return None, "'can_do_map' ids must be non-empty strings"
        if new in value:
            return None, f"'can_do_map' maps merged id {new} again; it must be one hop"
    return dict(value), None


def parse_statements(value: Any) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """``(statements, None)`` or ``(None, error)``; absent is empty."""
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return None, "'statements' must be an object of Can-Do id to statement"
    if len(value) > 2 * MAX_MAP_ENTRIES:
        return None, f"'statements' holds more than {2 * MAX_MAP_ENTRIES} ids"
    for lo_id, text in value.items():
        if _valid_id(lo_id) is None:
            return None, "'statements' ids must be non-empty strings"
        if not isinstance(text, str) or len(text) > MAX_STATEMENT_LENGTH:
            return None, (
                f"'statements' values must be text of at most "
                f"{MAX_STATEMENT_LENGTH} characters"
            )
    return dict(value), None


def _valid_id(value: Any) -> Optional[str]:
    if isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH:
        return value
    return None


class MigrateRequest(NamedTuple):
    room_id: str
    can_do_map: Dict[str, str]
    dry_run: bool
    # The event ids the dry run read, by COPY and PINS; None in a dry run.
    expected: Optional[Dict[str, Optional[str]]]
    # Can-Do row id to its statement, old and merged; empty writes no wording.
    statements: Dict[str, str]
    # Every Can-Do id on the environment; None removes no unknown id.
    valid_ids: Optional[FrozenSet[str]] = None


def parse_request(body: Any) -> Tuple[Optional[MigrateRequest], Optional[str]]:
    """``(request, None)`` or ``(None, error)``."""
    if not isinstance(body, dict):
        return None, "Request body must be a JSON object"
    room_id = _valid_id(body.get("room_id"))
    if room_id is None or not room_id.startswith("!"):
        return None, "'room_id' must be a room ID"
    can_do_map, error = parse_can_do_map(body.get("can_do_map"))
    if can_do_map is None:
        return None, error
    statements, error = parse_statements(body.get("statements"))
    if statements is None:
        return None, error
    valid_ids, error = parse_valid_ids(body.get("valid_ids"), can_do_map)
    if error is not None:
        return None, error
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return None, "'dry_run' must be a boolean"
    if dry_run:
        return (
            MigrateRequest(room_id, can_do_map, True, None, statements, valid_ids),
            None,
        )
    expected = body.get("expected")
    if (
        not isinstance(expected, dict)
        or set(expected) != {COPY, PINS}
        or not all(value is None or _valid_id(value) for value in expected.values())
    ):
        return None, (
            "A write needs 'expected': the event ids its dry run read, "
            f"as {{'{COPY}': id or null, '{PINS}': id or null}}"
        )
    return (
        MigrateRequest(
            room_id, can_do_map, False, dict(expected), statements, valid_ids
        ),
        None,
    )


MAX_VALID_IDS = 100_000


def parse_valid_ids(
    value: Any, can_do_map: Mapping[str, str]
) -> Tuple[Optional[FrozenSet[str]], Optional[str]]:
    """``(ids or None, None)`` or ``(None, error)``; absent removes nothing.
    It must hold every merged id, so a wrong file can never empty a course."""
    if value is None:
        return None, None
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_VALID_IDS
        or not all(_valid_id(item) for item in value)
    ):
        return None, (f"'valid_ids' must be a list of 1 to {MAX_VALID_IDS} Can-Do ids")
    ids = frozenset(value)
    missing = sorted(set(can_do_map.values()) - ids)
    if missing:
        return None, (
            f"'valid_ids' leaves out {len(missing)} merged ids, e.g. {missing[0]}"
        )
    return ids, None


class Plan(NamedTuple):
    answer: Dict[str, Any]
    # The state events to send, in order.
    writes: List[Tuple[str, Dict[str, Any]]]


def plan_space(
    state: Mapping[Tuple[str, str], Any],
    can_do_map: Mapping[str, str],
    statements: Optional[Mapping[str, str]] = None,
    valid_ids: Optional[AbstractSet[str]] = None,
) -> Plan:
    """What the migration would do to one space, from its current state."""
    copy_event = state.get((COURSE_MISSIONS_STATE_EVENT_TYPE, ""))
    mode_event = state.get((TEACHER_MODE_STATE_EVENT_TYPE, ""))
    writes: List[Tuple[str, Dict[str, Any]]] = []
    copy_answer: Optional[Dict[str, Any]] = None
    if copy_event is not None:
        # Plain JSON: Synapse may hand over a frozen event (it freezes the
        # ones it creates while a check_event_allowed callback is
        # registered), whose lists are tuples.
        content = unfreeze(copy_event.content)
        copy_answer = {"event_id": copy_event.event_id}
        problem = copy_problem(content)
        if problem is None and user_power(state, copy_event.sender) < INSTRUCTOR_POWER:
            problem = "its sender is no longer a course admin"
        if problem is not None:
            # Readers already ignore it; it is left as it is.
            copy_answer.update(
                outcome="ignored_by_readers",
                reason=problem,
                old_ids_left=_old_ids_in_copy(content, can_do_map),
            )
        else:
            rewrite = rewrite_copy(content, can_do_map, statements, valid_ids)
            copy_answer.update(rewrite.report, revision=content["revision"])
            if rewrite.content is None:
                copy_answer["outcome"] = "unchanged"
            elif copy_problem(rewrite.content) is not None:
                # Never expected: a copy the app refuses hides the course's
                # list, so it is never written.
                report(
                    "migrate_course_can_dos: a rewrite fails the app's parse; "
                    "not written",
                    "error",
                    room_id=copy_event.room_id,
                    reason=copy_problem(rewrite.content),
                )
                copy_answer.update(
                    outcome="rewrite_failed",
                    old_ids_left=_old_ids_in_copy(content, can_do_map),
                )
            else:
                copy_answer.update(
                    outcome="rewrite", new_revision=rewrite.content["revision"]
                )
                writes.append((COURSE_MISSIONS_STATE_EVENT_TYPE, rewrite.content))
    pins_answer: Optional[Dict[str, Any]] = None
    if mode_event is not None:
        mode = unfreeze(mode_event.content)
        listed: List[str] = []
        copy_content = unfreeze(copy_event.content) if copy_event is not None else None
        missions = (
            copy_content.get("missions") if isinstance(copy_content, Mapping) else None
        )
        if isinstance(missions, list):
            listed = [
                entry["lo_id"]
                for entry in missions
                if isinstance(entry, Mapping) and isinstance(entry.get("lo_id"), str)
            ]
        rewrite = rewrite_pins(mode, can_do_map, listed, valid_ids)
        outcome = "unchanged" if rewrite.content is None else "rewrite"
        pins = mode.get(PINS_KEY) if isinstance(mode, Mapping) else None
        if pins is not None and not isinstance(pins, Mapping):
            # The app reads no pins from it; nothing to re-key.
            outcome = "unreadable"
        pins_answer = {
            "event_id": mode_event.event_id,
            "outcome": outcome,
            **rewrite.report,
        }
        if rewrite.content is not None:
            writes.append((TEACHER_MODE_STATE_EVENT_TYPE, rewrite.content))
    answer = {
        COPY: copy_answer,
        PINS: pins_answer,
        "expected": {
            COPY: copy_event.event_id if copy_event is not None else None,
            PINS: mode_event.event_id if mode_event is not None else None,
        },
        "writes": [event_type for event_type, _ in writes],
    }
    return Plan(answer, writes)


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


class MigrateCourseCanDos(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi):
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
        # silent-ok: the caller's auth failure, answered 401 (logged at INFO)
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
            return
        try:
            operator = requester.user.to_string()
            if not await self._api.is_user_admin(operator):
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return
            req, error = parse_request(await extract_body_json(request))
            if req is None:
                respond_with_json(request, 400, {"error": error}, send_cors=True)
                return
            code, answer = await self.apply(req, operator)
            respond_with_json(request, code, answer, send_cors=True)
        except Exception as e:
            report_failure("migrate_course_can_dos: the call failed", e)
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def apply(
        self, req: MigrateRequest, operator: str
    ) -> Tuple[int, Dict[str, Any]]:
        """The response for an authorized, parsed request."""
        room_id = req.room_id
        state = await self._api.get_room_state(room_id)
        if (EventTypes.Create, "") not in state:
            return (404, {"error": "No such room on this server"})
        plan = plan_space(state, req.can_do_map, req.statements, req.valid_ids)
        answer = {"room_id": room_id, "dry_run": req.dry_run, **plan.answer}
        _report_left(room_id, answer)
        if not plan.writes:
            return (200, {**answer, "sender": None, "changed": False})

        if req.expected is not None and req.expected != answer["expected"]:
            # A teacher edited the space since the operator's dry run: what
            # was approved is not what would be written.
            report(
                "migrate_course_can_dos: the space changed since its dry run; skipped",
                room_id=room_id,
            )
            return (
                409,
                {
                    **answer,
                    "error": "The space changed since the dry run",
                    "changed": False,
                },
            )

        instructors = joined_local_instructors(self._api, state)
        if not instructors:
            report(
                "migrate_course_can_dos: no local instructor can send the events; "
                "refused",
                room_id=room_id,
                writes=",".join(answer["writes"]),
            )
            return (
                409,
                {
                    **answer,
                    "error": "No local instructor can send the course events",
                    "sender": None,
                    "changed": False,
                },
            )
        sender = instructors[0]
        answer["sender"] = sender
        if req.dry_run:
            return (200, {**answer, "changed": False})

        written: List[str] = []
        for event_type, content in plan.writes:
            try:
                await self._api.create_and_send_event_into_room(
                    {
                        "type": event_type,
                        "state_key": "",
                        "room_id": room_id,
                        "sender": sender,
                        "content": content,
                    }
                )
            except AuthError as e:
                # The sender left or lost power since they were chosen.
                report(
                    "migrate_course_can_dos: the room refused a write",
                    "error",
                    room_id=room_id,
                    event_type=event_type,
                    errcode=e.errcode,
                    written=",".join(written),
                )
                return (
                    409,
                    {
                        **answer,
                        "error": "The room refused a write",
                        "errcode": e.errcode,
                        "refused": event_type,
                        "written": written,
                        "changed": bool(written),
                    },
                )
            except Exception as e:
                report_failure(
                    "migrate_course_can_dos: a write failed",
                    e,
                    room_id=room_id,
                    event_type=event_type,
                    written=",".join(written),
                )
                return (500, {"error": "Internal server error", "written": written})
            written.append(event_type)

        copy_answer = answer[COPY] or {}
        for trimmed in copy_answer.get("notes_trimmed", []):
            # Ids, lengths and counts: never a teacher's words.
            logger.warning(
                "migrate_course_can_dos: room=%s lo_id=%s note trimmed: "
                "kept_length=%d dropped_count=%d dropped_length=%d",
                room_id,
                trimmed["lo_id"],
                trimmed["kept_length"],
                trimmed["dropped_count"],
                trimmed["dropped_length"],
            )
        _report_dropped(room_id, answer)
        logger.info(
            "migrate_course_can_dos: operator=%s room=%s writes=%s sent_as=%s "
            "combined=%d notes_added=%d notes_trimmed=%d pins_pooled=%d",
            operator,
            room_id,
            written,
            sender,
            len(copy_answer.get("combined", [])),
            len(copy_answer.get("notes_added", [])),
            len(copy_answer.get("notes_trimmed", [])),
            len((answer[PINS] or {}).get("pooled", [])),
        )
        return (200, {**answer, "changed": True})


def _report_left(room_id: str, answer: Mapping[str, Any]) -> None:
    """A space left with old ids, or a teacher's settings left out, reaches
    the team as well as the operator's report."""
    copy_answer = answer.get(COPY) or {}
    pins_answer = answer.get(PINS) or {}
    if copy_answer.get("old_ids_left"):
        report(
            "migrate_course_can_dos: a Can-Do list readers ignore still names old ids",
            room_id=room_id,
            outcome=copy_answer.get("outcome"),
            old_ids_left=copy_answer["old_ids_left"],
        )
    if pins_answer.get("outcome") == "unreadable":
        report(
            "migrate_course_can_dos: unreadable pins left as they are", room_id=room_id
        )


def _report_dropped(room_id: str, answer: Mapping[str, Any]) -> None:
    """What combining left out, once it is written."""
    copy_answer = answer.get(COPY) or {}
    pins_answer = answer.get(PINS) or {}
    dropped = {
        "due_dates_dropped": sum(
            c.get("due_dates_dropped", 0) for c in copy_answer.get("combined", [])
        ),
        "cities_dropped": sum(
            c.get("cities_dropped", 0) for c in copy_answer.get("combined", [])
        ),
        "fields_dropped": sum(
            len(c.get("fields_dropped", [])) for c in copy_answer.get("combined", [])
        ),
        "hidden_now_shown": sum(
            c.get("hidden_now_shown", 0) for c in copy_answer.get("combined", [])
        ),
        "removed_into_missions": len(copy_answer.get("removed_into_missions", [])),
        "notes_trimmed": len(copy_answer.get("notes_trimmed", [])),
        "unknown_removed": len(copy_answer.get("unknown_removed", []))
        + len(pins_answer.get("unknown_removed", [])),
        "pin_values_dropped": pins_answer.get("pin_values_dropped", 0),
        "unpinned_siblings": len(pins_answer.get("unpinned_siblings", [])),
    }
    if any(dropped.values()):
        report(
            "migrate_course_can_dos: combining left out teacher settings",
            room_id=room_id,
            **{key: value for key, value in dropped.items() if value},
        )
