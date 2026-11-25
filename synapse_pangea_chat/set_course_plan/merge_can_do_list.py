"""A course's Can-Do list brought in line with its quest's current choices
without losing anything learners or the teacher have in it.

Used by set_course_plan's ``keep_progress`` replacement. The quest's choices
were trimmed on purpose, and a course whose list still names the old ids shows
only the few the listing still returns. Writing the freshly built list instead
(the plain replacement) would also drop every old id, and the app credits a
Can-Do's XP only from the ids its list names, shown or in
``removed_activity_ids`` (client ``CourseMissionsEvent``, admin-dash
content-studio.instructions.md "No progress is lost"). So, per Can-Do:

- Shown activities become the freshly built entry's (the quest's chosen ids
  the library lists, or the builder's fallback when none is listed).
- Every id the old list named that is no longer shown moves to
  ``removed_activity_ids``, after the ones already there: hidden from
  students, still credited. A removed id the quest chooses again shows again.
- The entry's note is the course's when it has one (it may be the teacher's;
  a builder-written one cannot be told apart), else the quest's. ``due_at``
  and any other field of the entry are kept as they are.
- An old id that is hidden, private to someone else or otherwise not listed
  is never among the built entry's (the builder reads what the course listing
  lists), so it moves to ``removed_activity_ids`` like a dropped one.
- A Can-Do whose quest entry has nothing listable is left exactly as it is
  (``quest_empty``), never written empty: the dry run names it.
- A Can-Do the course has and the quest no longer has is kept unchanged, in
  place: learners may have progress on it. A Can-Do new to the quest is added
  after the course's, unless the course removed it (``removed_missions``).
- Every top-level field but ``missions`` is kept (``time_zone``,
  ``removed_missions``, anything newer); the caller sets the revision.

``losses`` compares the two lists directly, so "nothing lost" is reported only
when it is true, whatever the merge did.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import AbstractSet, Any, Dict, List, Mapping, Optional

# The only fields the server's builder writes. Content Studio adds the others
# (the course's time zone at first publish, due dates, removals), so a list
# with any other field is one a teacher published.
BUILDER_LIST_KEYS = frozenset({"version", "revision", "missions"})
BUILDER_ENTRY_KEYS = frozenset({"lo_id", "activity_ids", "note"})

FOLLOWS_QUEST = "follows_quest"
COURSE_ONLY = "course_only"
ADDED = "added"
REMOVED_BY_COURSE = "removed_by_course"
QUEST_EMPTY = "quest_empty"


def plain(value: Any) -> Any:
    """A plain copy of room state content, which Synapse freezes (mappings
    immutable, lists as tuples): the merge reads lists, and the copy written
    back must be ordinary JSON."""
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def _ids(value: Any) -> List[str]:
    """The string ids of a list field, de-duplicated in order."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(v for v in value if isinstance(v, str)))


def _missions(content: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    missions = (content or {}).get("missions")
    return [
        m
        for m in (missions if isinstance(missions, list) else [])
        if isinstance(m, Mapping) and isinstance(m.get("lo_id"), str)
    ]


def _removed_lo_ids(content: Mapping[str, Any]) -> set:
    removed = content.get("removed_missions")
    return {
        m["lo_id"]
        for m in (removed if isinstance(removed, list) else [])
        if isinstance(m, Mapping) and isinstance(m.get("lo_id"), str)
    }


def is_teacher_list(content: Optional[Mapping[str, Any]]) -> bool:
    """Whether the list has any field only Content Studio writes."""
    if not content:
        return False
    if set(content) - BUILDER_LIST_KEYS:
        return True
    return any(set(m) - BUILDER_ENTRY_KEYS for m in _missions(content))


@dataclass
class MergedCanDoList:
    content: Dict[str, Any]
    # Per Can-Do, in the merged list's order, then the quest's Can-Dos the
    # course removed: what the merge did, as counts.
    entries: List[Dict[str, Any]] = field(default_factory=list)
    lost: List[str] = field(default_factory=list)


def _follow(
    mission: Mapping[str, Any], quest_entry: Mapping[str, Any]
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    before = _ids(mission.get("activity_ids"))
    removed_before = _ids(mission.get("removed_activity_ids"))
    shown = _ids(quest_entry.get("activity_ids"))
    shown_set = set(shown)
    removed = [a for a in removed_before if a not in shown_set]
    moved = [a for a in before if a not in shown_set and a not in removed]
    removed += moved

    merged = dict(copy.deepcopy(mission))
    merged["activity_ids"] = shown
    merged.pop("removed_activity_ids", None)
    if removed:
        merged["removed_activity_ids"] = removed
    note: Optional[str] = None
    if isinstance(mission.get("note"), str):
        note = "course_kept" if mission.get("note") != quest_entry.get("note") else None
    elif isinstance(quest_entry.get("note"), str):
        merged["note"] = quest_entry["note"]
        note = "from_quest"
    return merged, {
        "lo_id": mission["lo_id"],
        "status": FOLLOWS_QUEST,
        "shown_before": len(before),
        "shown_after": len(shown),
        "moved_to_removed": len(moved),
        "back_to_shown": sum(1 for a in removed_before if a in shown_set),
        "removed_after": len(removed),
        "note": note,
        "shows_none": not shown,
    }


def merge_can_do_list(
    current: Mapping[str, Any], built: Mapping[str, Any]
) -> MergedCanDoList:
    """The course's list following ``built`` (the quest's list as the builder
    makes it), keeping everything ``current`` credits or holds."""
    quest = {m["lo_id"]: m for m in _missions(built)}
    course_removed = _removed_lo_ids(current)
    missions: List[Dict[str, Any]] = []
    entries: List[Dict[str, Any]] = []
    seen = set()
    for mission in _missions(current):
        lo_id = mission["lo_id"]
        if lo_id in seen:
            continue
        seen.add(lo_id)
        if lo_id in quest and _ids(quest[lo_id].get("activity_ids")):
            merged, entry = _follow(mission, quest[lo_id])
        else:
            merged = dict(copy.deepcopy(mission))
            shown = len(_ids(mission.get("activity_ids")))
            entry = {
                "lo_id": lo_id,
                "status": QUEST_EMPTY if lo_id in quest else COURSE_ONLY,
                "shown_before": shown,
                "shown_after": shown,
                "moved_to_removed": 0,
                "back_to_shown": 0,
                "removed_after": len(_ids(mission.get("removed_activity_ids"))),
                "note": None,
                "shows_none": not shown,
            }
        missions.append(merged)
        entries.append(entry)
    for lo_id, quest_entry in quest.items():
        if lo_id in seen:
            continue
        if lo_id in course_removed:
            entries.append({"lo_id": lo_id, "status": REMOVED_BY_COURSE})
            continue
        added = dict(copy.deepcopy(quest_entry))
        shown = len(_ids(added.get("activity_ids")))
        missions.append(added)
        entries.append(
            {
                "lo_id": lo_id,
                "status": ADDED,
                "shown_before": 0,
                "shown_after": shown,
                "moved_to_removed": 0,
                "back_to_shown": 0,
                "removed_after": 0,
                "note": "from_quest" if isinstance(added.get("note"), str) else None,
                "shows_none": not shown,
            }
        )
    content = {k: copy.deepcopy(v) for k, v in current.items() if k != "missions"}
    content["missions"] = missions
    return MergedCanDoList(content, entries, losses(current, content))


def shown_ids(content: Optional[Mapping[str, Any]]) -> List[str]:
    """Every id the list shows, once, in order."""
    return list(
        dict.fromkeys(
            a for m in _missions(content) for a in _ids(m.get("activity_ids"))
        )
    )


def totals(
    current: Mapping[str, Any],
    merged: MergedCanDoList,
    listable: Optional[AbstractSet[str]],
) -> Dict[str, Any]:
    """Per room, as the app shows it: an id counts as shown only when the
    course listing lists it (``listable``, None when that read failed). An
    entry that follows the quest shows the builder's ids, listed by
    construction; one left as it was shows what it showed."""
    status = {e["lo_id"]: e["status"] for e in merged.entries}

    def listed(mission: Mapping[str, Any], follows: bool) -> Optional[int]:
        ids = _ids(mission.get("activity_ids"))
        if follows:
            return len(ids)
        if listable is None:
            return None
        return sum(1 for a in ids if a in listable)

    before = [listed(m, False) for m in _missions(current)]
    after = [
        listed(m, status.get(m["lo_id"]) in (FOLLOWS_QUEST, ADDED))
        for m in _missions(merged.content)
    ]
    known_before = None if any(n is None for n in before) else before
    known_after = None if any(n is None for n in after) else after
    return {
        "entries": len(after),
        "shown_before": sum(known_before) if known_before is not None else None,
        "shown_after": sum(known_after) if known_after is not None else None,
        "moved_to_removed": sum(e.get("moved_to_removed", 0) for e in merged.entries),
        "empty_before": (
            sum(1 for n in known_before if n == 0) if known_before is not None else None
        ),
        "empty_after": (
            sum(1 for n in known_after if n == 0) if known_after is not None else None
        ),
    }


def losses(current: Mapping[str, Any], after: Mapping[str, Any]) -> List[str]:
    """Everything ``current`` holds that ``after`` does not: a Can-Do, a
    credited id (shown or removed), or any other field, top-level or per
    Can-Do. ``missions`` aside, a top-level field may only stay the same;
    ``revision`` is the caller's to raise."""
    lost: List[str] = []
    for key, value in current.items():
        if key in ("missions", "revision"):
            continue
        if after.get(key) != value:
            lost.append(f"list: {key} changed")
    later = {m["lo_id"]: m for m in _missions(after)}
    for mission in _missions(current):
        lo_id = mission["lo_id"]
        kept = later.get(lo_id)
        if kept is None:
            lost.append(f"{lo_id}: Can-Do dropped")
            continue
        credited = set(_ids(kept.get("activity_ids"))) | set(
            _ids(kept.get("removed_activity_ids"))
        )
        for activity_id in _ids(mission.get("activity_ids")) + _ids(
            mission.get("removed_activity_ids")
        ):
            if activity_id not in credited:
                lost.append(f"{lo_id}: activity {activity_id} no longer credited")
        for key, value in mission.items():
            if key in ("lo_id", "activity_ids", "removed_activity_ids"):
                continue
            if kept.get(key) != value:
                lost.append(f"{lo_id}: {key} changed")
    return lost
