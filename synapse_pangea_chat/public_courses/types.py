from typing import Any, Dict, List, Optional, TypedDict


class CourseFilters(TypedDict, total=False):
    target_language: str


class CourseAdmin(TypedDict):
    """Someone who runs a course: a course admin who is not a bot and is
    still in it, with the name and avatar (an mxc URI) they show there."""

    user_id: str
    display_name: Optional[str]
    avatar_url: Optional[str]


class CourseInstitution(TypedDict):
    """The school a course names: its display name and, when one was picked,
    its ROR or NCES id."""

    name: str
    id: Optional[str]


class Course(TypedDict):
    avatar_url: Optional[str]
    central: bool
    canonical_alias: Optional[str]
    course_id: Optional[str]
    guest_can_join: bool
    join_rule: Optional[str]
    name: Optional[str]
    num_joined_members: int
    room_id: str
    room_type: Optional[str]
    target_language: Optional[str]
    topic: Optional[str]
    world_readable: bool
    course_missions: Optional[Dict[str, Any]]
    course_position: Optional[Dict[str, Any]]
    admins: List[CourseAdmin]
    institution: Optional[CourseInstitution]


class PublicCoursesResponse(TypedDict):
    chunk: List[Course]
    next_batch: Optional[str]
    total_room_count_estimate: Optional[int]
