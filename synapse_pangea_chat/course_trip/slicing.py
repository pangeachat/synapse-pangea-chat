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

from synapse_pangea_chat.course_trip.cities import City, CityIndex, shared_city_list

TRIP_VERSION = 2
#: A Can-Do with this many pinned activities or more is viable; fewer, thin.
VIABLE_PINS = 3

VIABLE = "viable"
THIN = "thin"
EMPTY = "empty"


def _plan(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    res = doc.get("res")
    plan = res.get("plan") if isinstance(res, Mapping) else None
    return plan if isinstance(plan, Mapping) else {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def activity_point(doc: Mapping[str, Any]) -> Optional["tuple[float, float]"]:
    """``(lat, lng)`` from ``res.plan.coordinates`` (``[lng, lat]``), or None
    for an activity with no usable position. ``[0, 0]`` is the unplaced
    sentinel, never a place."""
    coordinates = _plan(doc).get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        return None
    lng = _number(coordinates[0])
    lat = _number(coordinates[1])
    if lat is None or lng is None or not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return None
    if lat == 0.0 and lng == 0.0:
        return None
    return lat, lng


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
    """Builds the version 2 copy from a version 1 copy's content and the
    library's activity documents."""

    def __init__(self, index: CityIndex) -> None:
        self._index = index

    def slice_copy(
        self,
        content: Mapping[str, Any],
        activity_docs: Sequence[Mapping[str, Any]],
        existing: Optional[Mapping[str, Any]] = None,
    ) -> "tuple[Dict[str, Any], List[Slice]]":
        """``content`` (a version 1 copy as the reader builds it: each Can-Do
        with every library activity) as the trip: each Can-Do sliced, the
        existing copy's notes, due dates, removed lists and start date kept,
        at the existing copy's revision plus one. Returns the copy and each
        Can-Do's slice, for the report."""
        points: Dict[str, "tuple[float, float]"] = {}
        for doc in activity_docs:
            activity_id = _plan(doc).get("activity_id")
            point = activity_point(doc)
            if isinstance(activity_id, str) and activity_id and point is not None:
                points[activity_id] = point
        kept: Dict[str, Mapping[str, Any]] = {}
        if existing is not None:
            for entry in existing.get("missions") or []:
                if isinstance(entry, Mapping) and isinstance(entry.get("lo_id"), str):
                    kept[entry["lo_id"]] = entry
        missions: List[Dict[str, Any]] = []
        slices: List[Slice] = []
        for entry in content.get("missions") or []:
            lo_id = entry["lo_id"]
            library = [a for a in entry.get("activity_ids", []) if isinstance(a, str)]
            previous = kept.get(lo_id)
            listed = (
                [a for a in previous.get("activity_ids", []) if isinstance(a, str)]
                if previous is not None
                else []
            )
            # The library's activities decide the city; the course's own listed
            # ones (a teacher's copies among them) stay pinned at it.
            candidates = list(dict.fromkeys([*library, *listed]))
            sliced = slice_can_do(lo_id, candidates, points, self._index)
            slices.append(sliced)
            mission: Dict[str, Any] = {
                "lo_id": lo_id,
                "activity_ids": sliced.activity_ids,
            }
            if sliced.city is not None:
                mission["city"] = sliced.city.as_mission_city()
            if previous is not None:
                for field in ("note", "due_at", "removed_activity_ids"):
                    if previous.get(field):
                        mission[field] = previous[field]
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
    """The generation backlog: each Can-Do's city, pins and status, and the
    totals (viable at three pinned or more, thin at one or two, empty)."""
    rows = [
        {
            "lo_id": s.lo_id,
            "city": s.city.name if s.city is not None else None,
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


def trip_slicer(api: Any, config: Any) -> Optional[Callable[[], Awaitable[TripSlicer]]]:
    """What a course-copy reader slices new copies with, when
    ``course_missions_v2_enabled``; None leaves it writing version 1."""
    if not getattr(config, "course_missions_v2_enabled", False):
        return None
    cities = shared_city_list(api, config)

    async def make() -> TripSlicer:
        return TripSlicer(await cities.index())

    return make
