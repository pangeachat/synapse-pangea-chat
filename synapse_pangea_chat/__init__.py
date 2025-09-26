from typing import Any, Dict

from synapse.module_api import ModuleApi

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.public_courses import PublicCourses


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

        # Register the HTTP endpoint for public_courses
        self._api.register_web_resource(
            path="/_synapse/client/unstable/org.pangea/public_courses",
            resource=self.public_courses,
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
