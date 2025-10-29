from typing import List, Optional, TypedDict


class Course(TypedDict):
    avatar_url: Optional[str]
    canonical_alias: Optional[str]
    course_id: Optional[str]
    guest_can_join: bool
    join_rule: Optional[str]
    name: Optional[str]
    num_joined_members: int
    room_id: str
    room_type: Optional[str]
    topic: Optional[str]
    world_readable: bool


class PublicCoursesResponse(TypedDict):
    chunk: List[Course]
    next_batch: Optional[str]
    prev_batch: Optional[str]
    total_room_count_estimate: Optional[int]
