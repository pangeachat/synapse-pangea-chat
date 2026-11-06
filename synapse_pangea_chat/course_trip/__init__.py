"""A course as a trip (pangeachat/.github#780; design: course-trip.instructions.md).

Three pieces: the listed cities and the one rule that names a point by them
(``cities``), a course copy sliced into one city per Can-Do (``slicing``, the
operator endpoint ``slice_course``), and the ship's position on an undated
course (``position``: the scheduled pass and its operator endpoint).
"""

from synapse_pangea_chat.course_trip.cities import (
    CityIndex,
    CityListReader,
    shared_city_list,
)
from synapse_pangea_chat.course_trip.position import (
    COURSE_POSITION_STATE_EVENT_TYPE,
    CoursePositionPass,
    CoursePositionRun,
)
from synapse_pangea_chat.course_trip.slice_course import AutoSlice, SliceCourse
from synapse_pangea_chat.course_trip.slicing import TripSlicer, trip_slicer

__all__ = [
    "AutoSlice",
    "COURSE_POSITION_STATE_EVENT_TYPE",
    "CityIndex",
    "CityListReader",
    "CoursePositionPass",
    "CoursePositionRun",
    "SliceCourse",
    "TripSlicer",
    "shared_city_list",
    "trip_slicer",
]
