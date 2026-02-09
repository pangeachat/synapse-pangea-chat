import logging
import re
from typing import Any, Dict, Mapping, Tuple

from synapse.events import EventBase
from synapse.module_api import ModuleApi

from synapse_pangea_chat.auto_accept_invite import AutoAcceptInviteIfKnocked
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.delete_room import DeleteRoom
from synapse_pangea_chat.limit_user_directory import LimitUserDirectory
from synapse_pangea_chat.public_courses import PublicCourses
from synapse_pangea_chat.room_code import KnockWithCode, RequestRoomCode
from synapse_pangea_chat.room_preview import (
    PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    PANGEA_ACTIVITY_ROLE_STATE_EVENT_TYPE,
    PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
    RoomPreview,
    invalidate_room_cache,
)

logger = logging.getLogger(f"synapse.module.{__name__}")


class PangeaChat:
    """
    Unified Pangea Chat module for Synapse.

    Composes all previously separate synapse modules:
    - PublicCourses: public course listing endpoint
    - RoomPreview: room state preview endpoint
    - KnockWithCode / RequestRoomCode: room code invitation endpoints
    - AutoAcceptInviteIfKnocked: auto-accept invites for users who knocked
    - DeleteRoom: room deletion endpoint
    - LimitUserDirectory: user directory spam filtering
    """

    def __init__(self, config: PangeaChatConfig, api: ModuleApi):
        self._api = api
        self._config = config

        # --- Public Courses ---
        self.public_courses = PublicCourses(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/unstable/org.pangea/public_courses",
            resource=self.public_courses,
        )

        # --- Room Preview ---
        self.room_preview_resource = RoomPreview(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/unstable/org.pangea/room_preview",
            resource=self.room_preview_resource,
        )

        # Register reactive cache invalidation callback for room preview
        self._api.register_third_party_rules_callbacks(
            on_new_event=self._on_new_event_room_preview,
        )

        # --- Room Code ---
        self.knock_with_code_resource = KnockWithCode(api, config)
        self.request_code_resource = RequestRoomCode(api, config)
        api.register_web_resource(
            path="/_synapse/client/pangea/v1/knock_with_code",
            resource=self.knock_with_code_resource,
        )
        api.register_web_resource(
            path="/_synapse/client/pangea/v1/request_room_code",
            resource=self.request_code_resource,
        )

        # --- Auto Accept Invite If Knocked ---
        self.auto_accept_invite = AutoAcceptInviteIfKnocked(config, api)

        # --- Delete Room ---
        self.delete_room_resource = DeleteRoom(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/delete_room",
            resource=self.delete_room_resource,
        )

        # --- Limit User Directory ---
        if config.limit_user_directory_public_attribute_search_path is not None:
            self.limit_user_directory = LimitUserDirectory(config, api)

    async def _on_new_event_room_preview(
        self,
        event: EventBase,
        _: Mapping[Tuple[str, str], EventBase],
    ) -> None:
        """
        Handle new events to reactively invalidate room preview cache
        when relevant state events change.
        """
        if not event.is_state():
            return

        if event.type not in self._config.set_room_preview_state_event_types:
            return

        room_id = event.room_id
        invalidate_room_cache(room_id)

    @staticmethod
    def parse_config(config: Dict[str, Any]) -> PangeaChatConfig:
        # --- public_courses config ---
        public_courses_burst_duration_seconds = config.get(
            "public_courses_burst_duration_seconds", 120
        )
        if public_courses_burst_duration_seconds < 1:
            raise ValueError("public_courses_burst_duration_seconds must be >= 1")

        public_courses_requests_per_burst = config.get(
            "public_courses_requests_per_burst", 120
        )
        if public_courses_requests_per_burst < 1:
            raise ValueError("public_courses_requests_per_burst must be >= 1")

        course_plan_state_event_type = config.get("course_plan_state_event_type", None)

        # --- room_preview config ---
        room_preview_state_event_types = config.get(
            "room_preview_state_event_types", ["p.room_summary"]
        )
        if not isinstance(room_preview_state_event_types, list):
            room_preview_state_event_types = ["p.room_summary"]

        # Always include PANGEA state event types
        pangea_types = [
            PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
            PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
            PANGEA_ACTIVITY_ROLE_STATE_EVENT_TYPE,
        ]
        all_event_types = list(set(room_preview_state_event_types + pangea_types))

        room_preview_burst_duration_seconds = config.get(
            "room_preview_burst_duration_seconds", 60
        )
        room_preview_requests_per_burst = config.get(
            "room_preview_requests_per_burst", 10
        )

        # --- room_code config ---
        knock_with_code_requests_per_burst = config.get(
            "knock_with_code_requests_per_burst", 10
        )
        knock_with_code_burst_duration_seconds = config.get(
            "knock_with_code_burst_duration_seconds", 60
        )

        # --- auto_accept_invite config ---
        auto_accept_invite_worker = config.get("auto_accept_invite_worker", None)

        # --- delete_room config ---
        delete_room_requests_per_burst = config.get(
            "delete_room_requests_per_burst", 10
        )
        delete_room_burst_duration_seconds = config.get(
            "delete_room_burst_duration_seconds", 60
        )

        # --- limit_user_directory config ---
        limit_user_directory_public_attribute_search_path = config.get(
            "limit_user_directory_public_attribute_search_path", None
        )
        if limit_user_directory_public_attribute_search_path is not None:
            if not isinstance(limit_user_directory_public_attribute_search_path, str):
                raise ValueError(
                    'Config "limit_user_directory_public_attribute_search_path" must be a string'
                )
            if (
                re.match(
                    r"^[a-z0-9_]+(\.[a-z0-9_]+)*$",
                    limit_user_directory_public_attribute_search_path,
                )
                is None
            ):
                raise ValueError(
                    'Config "limit_user_directory_public_attribute_search_path" must be in dot-syntax (i.e. profile.user_settings.public)'
                )

        limit_user_directory_whitelist_requester_id_patterns = config.get(
            "limit_user_directory_whitelist_requester_id_patterns", []
        )
        if not isinstance(limit_user_directory_whitelist_requester_id_patterns, list):
            raise ValueError(
                'Config "limit_user_directory_whitelist_requester_id_patterns" must be a list'
            )
        for pattern in limit_user_directory_whitelist_requester_id_patterns:
            if not isinstance(pattern, str):
                raise ValueError(
                    'Config "limit_user_directory_whitelist_requester_id_patterns" must be a list of strings'
                )

        limit_user_directory_filter_search_if_missing_public_attribute = config.get(
            "limit_user_directory_filter_search_if_missing_public_attribute", True
        )
        if not isinstance(
            limit_user_directory_filter_search_if_missing_public_attribute, bool
        ):
            raise ValueError(
                'Config "limit_user_directory_filter_search_if_missing_public_attribute" must be a boolean'
            )

        return PangeaChatConfig(
            public_courses_burst_duration_seconds=public_courses_burst_duration_seconds,
            public_courses_requests_per_burst=public_courses_requests_per_burst,
            course_plan_state_event_type=course_plan_state_event_type,
            room_preview_state_event_types=all_event_types,
            room_preview_burst_duration_seconds=room_preview_burst_duration_seconds,
            room_preview_requests_per_burst=room_preview_requests_per_burst,
            knock_with_code_requests_per_burst=knock_with_code_requests_per_burst,
            knock_with_code_burst_duration_seconds=knock_with_code_burst_duration_seconds,
            auto_accept_invite_worker=auto_accept_invite_worker,
            delete_room_requests_per_burst=delete_room_requests_per_burst,
            delete_room_burst_duration_seconds=delete_room_burst_duration_seconds,
            limit_user_directory_public_attribute_search_path=limit_user_directory_public_attribute_search_path,
            limit_user_directory_whitelist_requester_id_patterns=limit_user_directory_whitelist_requester_id_patterns,
            limit_user_directory_filter_search_if_missing_public_attribute=limit_user_directory_filter_search_if_missing_public_attribute,
        )
