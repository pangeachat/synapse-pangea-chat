import re
from typing import Any, Dict, Mapping, Tuple

from synapse.events import EventBase
from synapse.module_api import ModuleApi

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.delete_room import DeleteRoom
from synapse_pangea_chat.delete_user import DeleteUser
from synapse_pangea_chat.direct_push import DirectPush
from synapse_pangea_chat.email_invite import CreateCourseSpace, InviteByEmail
from synapse_pangea_chat.export_user_data import ExportUserData
from synapse_pangea_chat.limit_user_directory import LimitUserDirectory
from synapse_pangea_chat.public_courses import PublicCourses
from synapse_pangea_chat.register_email import RegisterEmailRequestToken
from synapse_pangea_chat.room_code import KnockWithCode, RequestRoomCode
from synapse_pangea_chat.room_preview import (
    PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    PANGEA_ACTIVITY_ROLE_STATE_EVENT_TYPE,
    PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
    RoomPreview,
    invalidate_room_cache,
)
from synapse_pangea_chat.user_activity import (
    CourseActivities,
    UserActivity,
    UserCourses,
)
from synapse_pangea_chat.user_directory_search import UserDirectorySearch


class PangeaChat:
    """
    Unified Pangea Chat module for Synapse.

    Composes all previously separate synapse modules:
    - PublicCourses: public course listing endpoint
    - RoomPreview: room state preview endpoint
    - KnockWithCode / RequestRoomCode: room code invitation endpoints
    - DeleteRoom: room deletion endpoint
    - LimitUserDirectory: user directory spam filtering
    """

    def __init__(self, config: PangeaChatConfig, api: ModuleApi):
        self._api = api
        self._config = config

        # --- Public Courses ---
        self.public_courses = PublicCourses(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/public_courses",
            resource=self.public_courses,
        )
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

        # --- Create Course Space ---
        self.create_course_space_resource = CreateCourseSpace(api, config)
        api.register_web_resource(
            path="/_synapse/client/pangea/v1/create_course_space",
            resource=self.create_course_space_resource,
        )

        # --- Invite By Email ---
        self.invite_by_email_resource = InviteByEmail(api, config)
        api.register_web_resource(
            path="/_synapse/client/pangea/v1/invite_by_email",
            resource=self.invite_by_email_resource,
        )

        # --- Delete Room ---
        self.delete_room_resource = DeleteRoom(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/delete_room",
            resource=self.delete_room_resource,
        )

        # --- Delete User ---
        self.delete_user_resource = DeleteUser(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/delete_user",
            resource=self.delete_user_resource,
        )

        # --- Export User Data ---
        self.export_user_data_resource = ExportUserData(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/export_user_data",
            resource=self.export_user_data_resource,
        )

        # --- User Activity ---
        self.user_activity_resource = UserActivity(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/user_activity",
            resource=self.user_activity_resource,
        )

        # --- Course Activities ---
        self.course_activities_resource = CourseActivities(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/course_activities",
            resource=self.course_activities_resource,
        )

        # --- User Courses ---
        self.user_courses_resource = UserCourses(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/user_courses",
            resource=self.user_courses_resource,
        )

        # --- Register Email ---
        self.register_email_resource = RegisterEmailRequestToken(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/register/email/requestToken",
            resource=self.register_email_resource,
        )

        # --- Direct Push ---
        self.direct_push_resource = DirectPush(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/send_push",
            resource=self.direct_push_resource,
        )

        # --- Limit User Directory ---
        if config.limit_user_directory_public_attribute_search_path is not None:
            # TODO(phase-out): Remove LimitUserDirectory spam-checker callback after
            # all clients are migrated to /_synapse/client/pangea/v1/user_directory/search.
            # Keeping both paths temporarily preserves backwards compatibility.
            self.limit_user_directory = LimitUserDirectory(config, api)

        # --- User Directory Search ---
        if config.limit_user_directory_public_attribute_search_path is not None:
            # TODO(phase-out): Once migration is complete, make this endpoint the
            # only supported directory search path and delete legacy callback wiring.
            self.user_directory_search_resource = UserDirectorySearch(api, config)
            self._api.register_web_resource(
                path="/_synapse/client/pangea/v1/user_directory/search",
                resource=self.user_directory_search_resource,
            )

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

        public_courses_cms_cache_ttl_seconds = config.get(
            "public_courses_cms_cache_ttl_seconds", 5
        )
        if (
            not isinstance(public_courses_cms_cache_ttl_seconds, int)
            or public_courses_cms_cache_ttl_seconds < 1
        ):
            raise ValueError(
                "public_courses_cms_cache_ttl_seconds must be an integer >= 1"
            )

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

        # --- delete_room config ---
        delete_room_requests_per_burst = config.get(
            "delete_room_requests_per_burst", 10
        )
        delete_room_burst_duration_seconds = config.get(
            "delete_room_burst_duration_seconds", 60
        )

        # --- user_activity config ---
        user_activity_requests_per_burst = config.get(
            "user_activity_requests_per_burst", 10
        )
        user_activity_burst_duration_seconds = config.get(
            "user_activity_burst_duration_seconds", 60
        )

        # --- delete_user config ---
        delete_user_requests_per_burst = config.get("delete_user_requests_per_burst", 5)
        delete_user_burst_duration_seconds = config.get(
            "delete_user_burst_duration_seconds", 60
        )
        delete_user_schedule_delay_seconds = config.get(
            "delete_user_schedule_delay_seconds", 7 * 24 * 60 * 60
        )
        delete_user_processor_interval_seconds = config.get(
            "delete_user_processor_interval_seconds", 60
        )

        # --- export_user_data config ---
        export_user_data_requests_per_burst = config.get(
            "export_user_data_requests_per_burst", 3
        )
        export_user_data_burst_duration_seconds = config.get(
            "export_user_data_burst_duration_seconds", 60
        )
        export_user_data_processor_interval_seconds = config.get(
            "export_user_data_processor_interval_seconds", 60
        )
        export_user_data_output_dir = config.get(
            "export_user_data_output_dir", "/tmp/pangea-export-user-data"
        )
        if not isinstance(export_user_data_output_dir, str):
            raise ValueError('Config "export_user_data_output_dir" must be a string')
        if not export_user_data_output_dir.strip():
            raise ValueError('Config "export_user_data_output_dir" cannot be empty')

        cms_base_url = config.get("cms_base_url")
        if not isinstance(cms_base_url, str):
            raise ValueError('Config "cms_base_url" is required and must be a string')
        if not cms_base_url.strip():
            raise ValueError('Config "cms_base_url" cannot be empty')

        cms_service_api_key = config.get("cms_service_api_key")
        if not isinstance(cms_service_api_key, str):
            raise ValueError(
                'Config "cms_service_api_key" is required and must be a string'
            )
        if not cms_service_api_key.strip():
            raise ValueError('Config "cms_service_api_key" cannot be empty')

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

        # --- user_directory_search config ---
        user_directory_search_requests_per_burst = config.get(
            "user_directory_search_requests_per_burst", 10
        )
        user_directory_search_burst_duration_seconds = config.get(
            "user_directory_search_burst_duration_seconds", 60
        )

        # --- register_email config ---
        register_email_requests_per_burst = config.get(
            "register_email_requests_per_burst", 5
        )
        register_email_burst_duration_seconds = config.get(
            "register_email_burst_duration_seconds", 60
        )

        # --- invite_by_email config ---
        invite_by_email_requests_per_burst = config.get(
            "invite_by_email_requests_per_burst", 5
        )
        invite_by_email_burst_duration_seconds = config.get(
            "invite_by_email_burst_duration_seconds", 60
        )
        app_base_url = config.get(
            "app_base_url", "https://app.pangea.chat"
        )

        return PangeaChatConfig(
            public_courses_burst_duration_seconds=public_courses_burst_duration_seconds,
            public_courses_requests_per_burst=public_courses_requests_per_burst,
            course_plan_state_event_type=course_plan_state_event_type,
            public_courses_cms_cache_ttl_seconds=public_courses_cms_cache_ttl_seconds,
            room_preview_state_event_types=all_event_types,
            room_preview_burst_duration_seconds=room_preview_burst_duration_seconds,
            room_preview_requests_per_burst=room_preview_requests_per_burst,
            knock_with_code_requests_per_burst=knock_with_code_requests_per_burst,
            knock_with_code_burst_duration_seconds=knock_with_code_burst_duration_seconds,
            delete_room_requests_per_burst=delete_room_requests_per_burst,
            delete_room_burst_duration_seconds=delete_room_burst_duration_seconds,
            user_activity_requests_per_burst=user_activity_requests_per_burst,
            user_activity_burst_duration_seconds=user_activity_burst_duration_seconds,
            delete_user_requests_per_burst=delete_user_requests_per_burst,
            delete_user_burst_duration_seconds=delete_user_burst_duration_seconds,
            delete_user_schedule_delay_seconds=delete_user_schedule_delay_seconds,
            delete_user_processor_interval_seconds=delete_user_processor_interval_seconds,
            export_user_data_requests_per_burst=export_user_data_requests_per_burst,
            export_user_data_burst_duration_seconds=export_user_data_burst_duration_seconds,
            export_user_data_processor_interval_seconds=export_user_data_processor_interval_seconds,
            export_user_data_output_dir=export_user_data_output_dir,
            cms_base_url=cms_base_url,
            cms_service_api_key=cms_service_api_key,
            limit_user_directory_public_attribute_search_path=limit_user_directory_public_attribute_search_path,
            limit_user_directory_whitelist_requester_id_patterns=limit_user_directory_whitelist_requester_id_patterns,
            limit_user_directory_filter_search_if_missing_public_attribute=limit_user_directory_filter_search_if_missing_public_attribute,
            user_directory_search_requests_per_burst=user_directory_search_requests_per_burst,
            user_directory_search_burst_duration_seconds=user_directory_search_burst_duration_seconds,
            register_email_requests_per_burst=register_email_requests_per_burst,
            register_email_burst_duration_seconds=register_email_burst_duration_seconds,
            invite_by_email_requests_per_burst=invite_by_email_requests_per_burst,
            invite_by_email_burst_duration_seconds=invite_by_email_burst_duration_seconds,
            app_base_url=app_base_url,
        )
