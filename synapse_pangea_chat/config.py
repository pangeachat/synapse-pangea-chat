"""
Config for the Pangea Chat module.

Unified configuration combining all previously separate synapse module configs:
- public_courses (original synapse-pangea-chat)
- room_preview (from synapse-room-preview)
- room_code (from synapse-room-code)
- auto_accept_invite (from synapse-auto-accept-invite-if-knocked)
- delete_room (from synapse-delete-room-rest-api)
- limit_user_directory (from synapse-limit-user-directory)
"""

from typing import List, Optional

import attr


@attr.s(auto_attribs=True, frozen=True)
class PangeaChatConfig:
    """Unified config for all Pangea Chat synapse modules."""

    # --- public_courses config ---
    public_courses_burst_duration_seconds: int = 120
    public_courses_requests_per_burst: int = 120
    course_plan_state_event_type: Optional[str] = None

    # --- room_preview config ---
    room_preview_state_event_types: List[str] = attr.Factory(list)
    room_preview_burst_duration_seconds: int = 60
    room_preview_requests_per_burst: int = 10

    _set_room_preview_state_event_types: Optional[set] = None

    @property
    def set_room_preview_state_event_types(self) -> set:
        if self._set_room_preview_state_event_types is not None:
            return self._set_room_preview_state_event_types
        return set(self.room_preview_state_event_types)

    # --- room_code config ---
    knock_with_code_requests_per_burst: int = 10
    knock_with_code_burst_duration_seconds: int = 60

    # --- auto_accept_invite config ---
    auto_accept_invite_worker: Optional[str] = None
    auto_invite_knocker_enabled: bool = False

    # --- delete_room config ---
    delete_room_requests_per_burst: int = 10
    delete_room_burst_duration_seconds: int = 60

    # --- limit_user_directory config ---
    limit_user_directory_public_attribute_search_path: Optional[str] = None
    limit_user_directory_whitelist_requester_id_patterns: List[str] = attr.Factory(list)
    limit_user_directory_filter_search_if_missing_public_attribute: bool = True
