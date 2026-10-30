"""A course copy sliced into one city per Can-Do (org quests doc, "A course
is a trip"; pangeachat/.github#780).

The pins decide the city: a Can-Do's city is the listed city most of its
activities fall under (ties to the first), and its pins are the activities
under that city. A Can-Do with no activity under a listed city keeps every
activity and has no city; it is still playable and does not move the ship.
The copy is version 2 and undated (cities open by progress) unless the
existing copy already runs by dates.
"""

from __future__ import annotations

import logging

from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
)

from synapse_pangea_chat.course_trip.cities import (
    City,
    CityIndex,
    CityListError,
    shared_city_list,
)

logger = logging.getLogger("synapse.module.synapse_pangea_chat.course_trip.slicing")

TRIP_VERSION = 2
#: A Can-Do with this many pinned activities or more is viable; fewer, thin.
VIABLE_PINS = 3

VIABLE = "viable"
THIN = "thin"
EMPTY = "empty"


class Slice(NamedTuple):
    """One Can-Do's slice: its pins and the city they fall under."""

    lo_id: str
    activity_ids: List[str]
    city: Optional[City]

    @property
    def status(self) -> str:
        n = len(self.activity_ids)
        return VIABLE if n >= VIABLE_PINS else THIN if n else EMPTY


def slice_can_do(
    lo_id: str,
    candidates: Sequence[str],
    points: Mapping[str, "tuple[float, float]"],
    index: CityIndex,
) -> Slice:
    """The slice for one Can-Do over ``candidates`` (its activities, in order):
    the listed city most of them fall under, ties to the first candidate's,
    and the candidates under it. A candidate with no known position is kept
    whatever the city, since it cannot be placed elsewhere; one placed under
    another city, or under no listed city, is left out."""
    counts: Dict[str, int] = {}
    cities: Dict[str, City] = {}
    #: Each placed candidate's city key; None for one under no listed city.
    placed: Dict[str, Optional[str]] = {}
    for activity_id in candidates:
        point = points.get(activity_id)
        if point is None:
            continue
        found = index.nearest(*point)
        placed[activity_id] = found.key if found is not None else None
        if found is None:
            continue
        counts[found.key] = counts.get(found.key, 0) + 1
        cities.setdefault(found.key, found)
    if not counts:
        return Slice(lo_id, list(dict.fromkeys(candidates)), None)
    best = next(iter(counts))
    for key, n in counts.items():
        if n > counts[best]:
            best = key
    pins = [
        activity_id
        for activity_id in dict.fromkeys(candidates)
        if activity_id not in placed or placed[activity_id] == best
    ]
    return Slice(lo_id, pins, cities[best])


class TripSlicer:
    """Builds the version 2 copy from a course's list and the positions of
    the activities on it."""

    def __init__(self, index: CityIndex) -> None:
        self._index = index

    def slice_copy(
        self,
        content: Mapping[str, Any],
        points: Mapping[str, "tuple[float, float]"],
        existing: Optional[Mapping[str, Any]] = None,
    ) -> "tuple[Dict[str, Any], List[Slice]]":
        """The trip, at the existing copy's revision plus one, and each
        Can-Do's slice for the report.

        Slicing narrows a course's own list to one city; it never adds to it.
        An existing copy is that list (content-studio doc, "The course's own
        copy"): its Can-Dos in its order, the teacher's added ones included and
        removed ones left out, each sliced over the activities it lists. An
        activity the slice leaves out joins the Can-Do's removed activities, so
        work already done on it keeps counting. With no copy (or an empty one)
        the course's list is ``content``, the quest's Can-Dos with their library
        activities. ``points`` holds each activity's ``(lat, lng)``; one it
        lacks has no known position and is kept wherever the city falls."""
        existing_entries = [
            entry
            for entry in (existing or {}).get("missions") or []
            if isinstance(entry, Mapping) and isinstance(entry.get("lo_id"), str)
        ]
        own_list = bool(existing_entries)
        entries = existing_entries or [
            entry
            for entry in content.get("missions") or []
            if isinstance(entry, Mapping) and isinstance(entry.get("lo_id"), str)
        ]
        missions: List[Dict[str, Any]] = []
        slices: List[Slice] = []
        for entry in entries:
            lo_id = entry["lo_id"]
            listed = [a for a in entry.get("activity_ids", []) if isinstance(a, str)]
            removed = [
                a for a in entry.get("removed_activity_ids") or [] if isinstance(a, str)
            ]
            sliced = slice_can_do(
                lo_id, [a for a in listed if a not in removed], points, self._index
            )
            slices.append(sliced)
            mission: Dict[str, Any] = {
                "lo_id": lo_id,
                "activity_ids": sliced.activity_ids,
            }
            if sliced.city is not None:
                mission["city"] = sliced.city.as_mission_city()
            if own_list:
                for field in ("note", "due_at"):
                    if entry.get(field):
                        mission[field] = entry[field]
                pinned = set(sliced.activity_ids)
                kept_credit = list(
                    dict.fromkeys([*removed, *(a for a in listed if a not in pinned)])
                )
                if kept_credit:
                    mission["removed_activity_ids"] = kept_credit
            missions.append(mission)
        revision = existing.get("revision") if existing is not None else None
        copy: Dict[str, Any] = {
            "version": TRIP_VERSION,
            "revision": (revision if isinstance(revision, int) and revision >= 1 else 0)
            + 1,
            "missions": missions,
        }
        if existing is not None:
            for field in ("time_zone", "start_date", "removed_missions"):
                if existing.get(field):
                    copy[field] = existing[field]
        return copy, slices


def report(slices: Sequence[Slice]) -> Dict[str, Any]:
    """The generation backlog: each Can-Do's city (by name, and by its CMS key,
    since names repeat), pins and status, and the totals (viable at three
    pinned or more, thin at one or two, empty)."""
    rows = [
        {
            "lo_id": s.lo_id,
            "city": s.city.name if s.city is not None else None,
            "city_key": s.city.key if s.city is not None else None,
            "pinned": len(s.activity_ids),
            "status": s.status,
        }
        for s in slices
    ]
    return {
        "can_dos": rows,
        "viable": sum(1 for r in rows if r["status"] == VIABLE),
        "thin": sum(1 for r in rows if r["status"] == THIN),
        "empty": sum(1 for r in rows if r["status"] == EMPTY),
    }


def trip_slicer(
    api: Any, config: Any
) -> Optional[Callable[[], Awaitable[Optional[TripSlicer]]]]:
    """What a course-copy reader slices new copies with, when
    ``course_missions_v2_enabled``; None leaves it writing version 1."""
    if not getattr(config, "course_missions_v2_enabled", False):
        return None
    cities = shared_city_list(api, config)

    async def make() -> Optional[TripSlicer]:
        try:
            return TripSlicer(await cities.index())
        except CityListError as e:
            # silent-ok: logged; the caller writes the copy unsliced and the slice pass gives it cities
            logger.warning("A new course copy is written unsliced: %s", e)
            return None

    return make
