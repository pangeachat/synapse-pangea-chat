"""
Config for the Pangea Chat module
"""

from typing import Optional

import attr


@attr.s(auto_attribs=True, frozen=True)
class PangeaChatConfig:
    """Config for the Pangea Chat module"""

    public_courses_burst_duration_seconds: int = 120
    public_courses_requests_per_burst: int = 120
    course_plan_state_event_type: Optional[str] = None
