"""Per-route request limits for the student invitation routes.

Each route reads ``<route>_requests_per_burst`` and
``<route>_burst_duration_seconds`` from the module config. Authenticated
routes count per caller; the two public routes count per client IP.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

# route name -> (requests per burst, burst duration in seconds)
DEFAULT_LIMITS: Dict[str, Tuple[int, int]] = {
    "student_invitations_add": (30, 60),
    "student_invitations_send": (20, 60),
    "student_invitations_list": (60, 60),
    "student_invitations_revoke": (60, 60),
    "student_invitations_pending_approvals": (60, 60),
    "student_invitations_decide": (60, 60),
    "student_invitations_approve_all": (10, 60),
    "student_invitations_invite_member": (60, 60),
    # choreo calls it once per seat assignment, with the teacher's token.
    "student_invitations_live": (300, 60),
    "student_invitations_confirm": (10, 60),
    "student_invitations_mine_pending": (60, 60),
    # choreo reads it with the student's token on a full gate recompute.
    "student_invitations_mine_joined": (120, 60),
    "student_invitations_hint": (10, 60),
    "managed_disclosure": (60, 60),
    # Canvas (CONTRACTS C5 L1-L2, C2 T10-T11). The link step may come without
    # a token (a bound ticket), so it counts per client IP.
    "lti_link": (30, 60),
    "lti_connect": (20, 60),
    "lti_course_status": (60, 60),
    "lti_import": (10, 60),
}


def parse_rate_limits(config: Mapping[str, Any]) -> Dict[str, Tuple[int, int]]:
    limits: Dict[str, Tuple[int, int]] = {}
    for route, (default_burst, default_seconds) in DEFAULT_LIMITS.items():
        values = []
        for suffix, default in (
            ("requests_per_burst", default_burst),
            ("burst_duration_seconds", default_seconds),
        ):
            key = f"{route}_{suffix}"
            value = config.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{key} must be an integer >= 1")
            values.append(value)
        limits[route] = (values[0], values[1])
    return limits


def limit_for(limits: Mapping[str, Tuple[int, int]], route: str) -> Tuple[int, int]:
    return limits.get(route, DEFAULT_LIMITS[route])
