"""Unit tests for the course trip (course-trip.instructions.md): the city
list by key, a Can-Do's city slice over the cities the CMS stores on each
activity, and the busiest stop an undated course's ship sits at."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from synapse_pangea_chat.course_trip.cities import CityIndex, city
from synapse_pangea_chat.course_trip.position import (
    Position,
    busiest_stop,
    is_undated_trip,
    member_stop,
    next_position,
    parse_position,
)
from synapse_pangea_chat.course_trip.slicing import (
    EMPTY,
    THIN,
    VIABLE,
    TripSlicer,
    report,
    slice_can_do,
)
from synapse_pangea_chat.email_invite.course_missions import activity_places

FIXTURES = Path(__file__).parent / "fixtures"
MEXICO_CITY = city("mx", "Mexico City", "Mexico", 19.4444, -99.1329)
MADRID = city("es", "Madrid", "Spain", 40.4, -3.6834)
CITIES = CityIndex([MEXICO_CITY, MADRID])


class TestCityList(unittest.TestCase):
    """The CMS list, read by key: the CMS names each activity's city, so
    nothing here matches a point."""

    @classmethod
    def setUpClass(cls) -> None:
        rows = json.loads((FIXTURES / "cities.json").read_text(encoding="utf-8"))
        cls.index = CityIndex.from_rows(rows)

    def test_the_list_is_the_seed(self) -> None:
        self.assertEqual(len(self.index), 515)

    def test_a_key_names_its_city(self) -> None:
        self.assertEqual(CITIES.city("mx"), MEXICO_CITY)
        self.assertIsNone(CITIES.city("gone"))
        self.assertIsNone(CITIES.city(None))

    def test_rows_that_cannot_be_placed_are_dropped(self) -> None:
        index = CityIndex.from_rows(
            [
                {"key": "a", "name": "A", "country": "X", "lat": 1.0, "lng": 1.0},
                {"key": "a", "name": "again", "country": "X", "lat": 2.0, "lng": 2.0},
                {"key": "b", "name": "", "country": "X", "lat": 1.0, "lng": 1.0},
                {"key": "c", "name": "C", "country": "X", "lat": "1", "lng": 1.0},
                {"key": "d", "name": "D", "country": "X", "lat": 91.0, "lng": 1.0},
                "not a row",
            ]
        )
        self.assertEqual([c.name for c in index.cities], ["A"])


def _doc(
    activity_id: str,
    lat: Optional[float],
    lng: Optional[float],
    city_key: Optional[str] = None,
) -> Dict[str, Any]:
    plan: Dict[str, Any] = {"activity_id": activity_id, "l2": "es"}
    if lat is not None and lng is not None:
        plan["coordinates"] = [lng, lat]
    return {"city_key": city_key, "res": {"plan": plan}}


class TestSlice(unittest.TestCase):
    # Each placed activity's stored city key; "far" is placed under no
    # listed city, and "none" has no known position.
    PLACES: Dict[str, Optional[str]] = {
        "m1": "mx",
        "m2": "mx",
        "s1": "es",
        "far": None,
    }

    def test_the_pins_decide_the_city_most_of_them_fall_under(self) -> None:
        sliced = slice_can_do(
            "lo", ["s1", "m1", "none", "far", "m2"], self.PLACES, CITIES
        )
        self.assertEqual(sliced.city, MEXICO_CITY)
        # The pins: the ones under the city, and one with no known position.
        self.assertEqual(sliced.activity_ids, ["m1", "none", "m2"])
        self.assertEqual(sliced.status, VIABLE)

    def test_a_tie_goes_to_the_first_candidates_city(self) -> None:
        self.assertEqual(
            slice_can_do("lo", ["s1", "m1"], self.PLACES, CITIES).city, MADRID
        )
        self.assertEqual(
            slice_can_do("lo", ["m1", "s1"], self.PLACES, CITIES).city, MEXICO_CITY
        )

    def test_no_listed_city_keeps_every_activity_and_names_none(self) -> None:
        sliced = slice_can_do("lo", ["none", "far"], self.PLACES, CITIES)
        self.assertIsNone(sliced.city)
        self.assertEqual(sliced.activity_ids, ["none", "far"])
        self.assertEqual(sliced.status, THIN)
        self.assertEqual(slice_can_do("lo", [], self.PLACES, CITIES).status, EMPTY)

    def test_the_copy_is_the_trip_keeping_the_existing_copys_edits(self) -> None:
        built = {
            "version": 1,
            "revision": 1,
            "missions": [
                {"lo_id": "lo1", "activity_ids": ["s1", "m1", "m2", "m3", "m4"]},
                {"lo_id": "lo2", "activity_ids": ["far"]},
                {"lo_id": "lo3", "activity_ids": ["m1"]},
                {"lo_id": "lo4", "activity_ids": ["m2"]},
            ],
        }
        places = activity_places(
            [
                _doc("m1", 19.35, -99.16, "mx"),
                _doc("m2", 19.50, -99.10, "mx"),
                _doc("m3", 19.40, -99.20, "mx"),
                _doc("m4", 19.45, -99.15, "mx"),
                _doc("s1", 40.42, -3.70, "es"),
                _doc("far", 0.5, 0.5),
                _doc("none", None, None),
            ]
        )
        existing = {
            "version": 1,
            "revision": 4,
            "time_zone": "America/Mexico_City",
            "missions": [
                # The teacher put lo2 first, removed lo3, and never had lo4
                # (a library addition since the copy was made).
                {"lo_id": "lo2", "activity_ids": ["far"]},
                # Madrid's "s1" is outvoted and leaves the pins but keeps its
                # credit; "m2" was removed and stays removed; the library's
                # "m4" never joins; the teacher's own "own1" (no position
                # known) stays pinned.
                {
                    "lo_id": "lo1",
                    "activity_ids": ["s1", "m1", "m3", "own1"],
                    "note": "Week 1",
                    "due_at": "2026-11-09T05:59:00Z",
                    "removed_activity_ids": ["m2"],
                },
                # A Can-Do the teacher added: kept, with its own activity.
                {"lo_id": "lo-own", "activity_ids": ["own2"]},
            ],
            "removed_missions": [
                {"lo_id": "lo3", "activity_ids": ["m1"], "shown_count": 1}
            ],
        }
        copy, slices = TripSlicer(CITIES).slice_copy(built, places, existing)
        self.assertEqual(copy["version"], 2)
        self.assertEqual(copy["revision"], 5)
        self.assertEqual(copy["time_zone"], "America/Mexico_City")
        self.assertNotIn("start_date", copy)
        self.assertEqual(copy["removed_missions"], existing["removed_missions"])
        self.assertEqual(
            [m["lo_id"] for m in copy["missions"]], ["lo2", "lo1", "lo-own"]
        )
        self.assertEqual(copy["missions"][0], {"lo_id": "lo2", "activity_ids": ["far"]})
        self.assertEqual(
            copy["missions"][1],
            {
                "lo_id": "lo1",
                "activity_ids": ["m1", "m3", "own1"],
                "city": {
                    "name": "Mexico City",
                    "lat_e6": 19444400,
                    "lng_e6": -99132900,
                },
                "note": "Week 1",
                "due_at": "2026-11-09T05:59:00Z",
                "removed_activity_ids": ["m2", "s1"],
            },
        )
        self.assertEqual(
            copy["missions"][2], {"lo_id": "lo-own", "activity_ids": ["own2"]}
        )
        summary = report(slices)
        self.assertEqual(
            (summary["viable"], summary["thin"], summary["empty"]), (1, 2, 0)
        )
        self.assertEqual(summary["can_dos"][1]["city"], "Mexico City")

    def test_without_a_course_list_every_quest_can_do_is_sliced(self) -> None:
        built = {
            "version": 1,
            "revision": 1,
            "missions": [
                {"lo_id": "lo1", "activity_ids": ["m1", "s1", "m2"]},
                {"lo_id": "lo2", "activity_ids": []},
            ],
        }
        places = activity_places(
            [
                _doc("m1", 19.35, -99.16, "mx"),
                _doc("m2", 19.50, -99.10, "mx"),
                _doc("s1", 40.42, -3.70, "es"),
            ]
        )
        for existing in (None, {"version": 1, "revision": 2, "missions": []}):
            copy, _ = TripSlicer(CITIES).slice_copy(built, places, existing)
            self.assertEqual([m["lo_id"] for m in copy["missions"]], ["lo1", "lo2"])
            # The library has no credit to keep: the other city's activity is
            # simply not pinned.
            self.assertEqual(
                copy["missions"][0],
                {
                    "lo_id": "lo1",
                    "activity_ids": ["m1", "m2"],
                    "city": MEXICO_CITY.as_mission_city(),
                },
            )

    def test_a_dated_copy_keeps_its_start_date(self) -> None:
        built = {
            "version": 1,
            "revision": 1,
            "missions": [{"lo_id": "lo1", "activity_ids": []}],
        }
        existing = {
            "version": 2,
            "revision": 2,
            "start_date": "2026-11-02",
            "missions": [],
        }
        copy, _ = TripSlicer(CITIES).slice_copy(built, {}, existing)
        self.assertEqual(copy["start_date"], "2026-11-02")
        self.assertEqual(copy["revision"], 3)

    def test_without_an_existing_copy_the_trip_starts_at_revision_1(self) -> None:
        built = {
            "version": 1,
            "revision": 1,
            "missions": [{"lo_id": "lo1", "activity_ids": []}],
        }
        copy, _ = TripSlicer(CITIES).slice_copy(built, {}, None)
        self.assertEqual(copy["revision"], 1)


MISSIONS: List[Dict[str, Any]] = [
    {
        "lo_id": "lo1",
        "activity_ids": ["a"],
        "city": {"name": "A", "lat_e6": 1, "lng_e6": 1},
    },
    {"lo_id": "lo2", "activity_ids": ["b"]},
    {
        "lo_id": "lo3",
        "activity_ids": ["c"],
        "city": {"name": "C", "lat_e6": 3, "lng_e6": 3},
    },
]
NOW = 1_800_000_000_000
WEEK = 7 * 86400 * 1000


def _progress(
    completed: List[str], computed_at: str = "2027-01-15T00:00:00Z"
) -> Dict[str, Any]:
    return {
        "computed_at": computed_at,
        "can_dos": {lo: {"completed": lo in completed} for lo in ("lo1", "lo2", "lo3")},
    }


class TestPosition(unittest.TestCase):
    NOW_ISO_MS = 1_800_000_000_000  # 2027-01-15T08:00:00Z

    def test_a_member_is_at_their_first_incomplete_mission(self) -> None:
        self.assertEqual(member_stop(MISSIONS, _progress([]), self.NOW_ISO_MS, WEEK), 0)
        self.assertEqual(
            member_stop(MISSIONS, _progress(["lo1"]), self.NOW_ISO_MS, WEEK), 1
        )
        # Every Mission complete: the last stop.
        self.assertEqual(
            member_stop(
                MISSIONS, _progress(["lo1", "lo2", "lo3"]), self.NOW_ISO_MS, WEEK
            ),
            2,
        )

    def test_only_an_active_member_counts(self) -> None:
        self.assertIsNone(member_stop(MISSIONS, None, self.NOW_ISO_MS, WEEK))
        stale = _progress([], computed_at="2026-12-01T00:00:00Z")
        self.assertIsNone(member_stop(MISSIONS, stale, self.NOW_ISO_MS, WEEK))
        self.assertIsNone(member_stop(MISSIONS, {"can_dos": {}}, self.NOW_ISO_MS, WEEK))

    def test_the_busiest_stop_ties_to_the_earlier_and_defaults_to_the_first(
        self,
    ) -> None:
        self.assertEqual(busiest_stop([2, 0, 2, 1]), 2)
        self.assertEqual(busiest_stop([2, 0, 1, 1, 2]), 1)
        self.assertEqual(busiest_stop([]), 0)

    def test_the_ship_sails_from_where_it_is_and_a_mission_with_no_city_does_not_move_it(
        self,
    ) -> None:
        first = next_position(None, 2, MISSIONS, NOW, 3)
        self.assertEqual(first, Position(2, 2, NOW, 3))
        self.assertIsNone(next_position(first, 2, MISSIONS, NOW + 1, 3))
        self.assertEqual(
            next_position(first, 0, MISSIONS, NOW + 1, 3), Position(0, 2, NOW + 1, 3)
        )
        # lo2 has no city: the ship stays.
        self.assertIsNone(next_position(first, 1, MISSIONS, NOW + 1, 3))

    def test_a_new_copy_revision_writes_a_fresh_position(self) -> None:
        # The same index under another revision may name another Mission, so
        # the ship is placed afresh rather than left at a stale index.
        at_two = Position(2, 0, NOW, 3)
        self.assertEqual(
            next_position(at_two, 2, MISSIONS, NOW + 1, 4), Position(2, 2, NOW + 1, 4)
        )
        # A stop the copy no longer has is placed afresh too.
        beyond = Position(9, 2, NOW, 3)
        self.assertEqual(
            next_position(beyond, 0, MISSIONS, NOW + 1, 3), Position(0, 0, NOW + 1, 3)
        )

    def test_a_position_is_four_non_negative_integers(self) -> None:
        good = {"stop": 1, "from_stop": 0, "since": 5, "revision": 2}
        self.assertEqual(parse_position(good), Position(1, 0, 5, 2))
        self.assertIsNone(parse_position({**good, "stop": "1"}))
        self.assertIsNone(parse_position({**good, "stop": -1}))
        self.assertIsNone(parse_position({**good, "revision": 0}))
        self.assertIsNone(parse_position({"stop": 1, "from_stop": 0, "since": 5}))
        self.assertIsNone(parse_position("x"))

    def test_only_an_undated_trip_gets_a_position(self) -> None:
        self.assertTrue(is_undated_trip({"version": 2, "missions": []}))
        self.assertFalse(
            is_undated_trip({"version": 2, "missions": [], "start_date": "2026-11-02"})
        )
        self.assertFalse(is_undated_trip({"version": 1, "missions": []}))
        self.assertFalse(is_undated_trip(None))
