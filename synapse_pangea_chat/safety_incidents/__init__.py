"""The Safety page's Synapse half (pangeachat/admin-dash#105).

`POST /_synapse/client/pangea/v1/report` records a learner's in-app report;
`GET /_synapse/client/pangea/v1/safety_incidents?space_id=` gives a course's
admins every incident that belongs to their course. The incidents themselves -
moderation's and reports' alike - live in `moderation.incidents`; the design is
in `.github/instructions/moderation.instructions.md`.
"""

from typing import Any

from synapse.module_api import ModuleApi

from synapse_pangea_chat.moderation.courses import StudentCourses
from synapse_pangea_chat.moderation.exempt import glob_match
from synapse_pangea_chat.moderation.incidents import IncidentStore
from synapse_pangea_chat.safety_incidents.handlers import ReadHandler, ReportHandler
from synapse_pangea_chat.safety_incidents.resources import (
    SafetyIncidents,
    SafetyReport,
    SlidingWindowLimit,
)
from synapse_pangea_chat.safety_incidents.startup import SafetyIncidentsStartup

REPORT_PATH = "/_synapse/client/pangea/v1/report"
INCIDENTS_PATH = "/_synapse/client/pangea/v1/safety_incidents"

__all__ = ["register_safety_incidents"]


def register_safety_incidents(api: ModuleApi, config: Any) -> None:
    """Register both endpoints, and - on the background-tasks instance - the
    one-time backfill and the startup sweep."""
    homeserver = api._hs
    globs = list(config.moderation_exempt_user_id_globs)

    def _is_exempt(user_id: str) -> bool:
        return any(glob_match(pattern, user_id) for pattern in globs)

    store = IncidentStore(homeserver)
    courses = StudentCourses.from_homeserver(homeserver, _is_exempt)
    api.register_web_resource(
        path=REPORT_PATH,
        resource=SafetyReport(
            homeserver,
            ReportHandler(homeserver, store, courses),
            SlidingWindowLimit(
                config.safety_report_requests_per_burst,
                config.safety_report_burst_duration_seconds,
            ),
        ),
    )
    api.register_web_resource(
        path=INCIDENTS_PATH,
        resource=SafetyIncidents(
            homeserver,
            ReadHandler(homeserver, store),
            SlidingWindowLimit(
                config.safety_incidents_requests_per_burst,
                config.safety_incidents_burst_duration_seconds,
            ),
        ),
    )
    if api.should_run_background_tasks():
        SafetyIncidentsStartup(homeserver, store, courses).schedule()
