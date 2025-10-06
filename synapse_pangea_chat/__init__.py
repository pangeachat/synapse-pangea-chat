from typing import Any, Dict, Mapping, Tuple

from synapse.module_api import ModuleApi

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.public_courses import PublicCourses
from synapse.events import EventBase

from synapse_pangea_chat.unassign_activity_role_on_leave import (
    unassigned_activity_role_on_leave,
)


class PangeaChat:
    """
    Pangea Chat module for Synapse.
    """

    def __init__(self, config: PangeaChatConfig, api: ModuleApi):
        # Keep a reference to the config and Module API
        self._api = api
        self._config = config

        # Initiate resources
        self.public_courses = PublicCourses(api, config)

        # Register reactive cache invalidation callback
        self._api.register_third_party_rules_callbacks(
            on_new_event=self._on_new_event,
        )

        # Register the HTTP endpoint for public_courses
        self._api.register_web_resource(
            path="/_synapse/client/unstable/org.pangea/public_courses",
            resource=self.public_courses,
        )

    async def _on_new_event(
        self,
        event: EventBase,
        _: Mapping[Tuple[str, str], EventBase],
    ) -> None:
        """
        Handle new events to reactively invalidate cache when relevant state events change.

        This callback is triggered for every new event in the homeserver.
        We only care about state events that match our configured preview types.
        """
        # Only process state events
        if not event.is_state():
            return

        # Only process events for types we care about
        if (
            event.type != "m.room.membership"
            and event.content.get("membership") != "leave"
        ):
            return

        await unassigned_activity_role_on_leave(
            event.room_id, event.state_key, self._api
        )

    @staticmethod
    def parse_config(config: Dict[str, Any]) -> PangeaChatConfig:
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

        return PangeaChatConfig(
            public_courses_burst_duration_seconds=public_courses_burst_duration_seconds,
            public_courses_requests_per_burst=public_courses_requests_per_burst,
        )
