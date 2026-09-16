import ipaddress
import logging
import re
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

from synapse.events import EventBase
from synapse.module_api import ModuleApi

from synapse_pangea_chat.activity_session_previews import ActivitySessionPreviews
from synapse_pangea_chat.assign_room_membership import AssignRoomMembership
from synapse_pangea_chat.blocked_join_gate import BlockedJoinGate
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.delayed_push import configure_delayed_push
from synapse_pangea_chat.delayed_push.delayed_push import AUDITED_SYNAPSE_VERSION
from synapse_pangea_chat.delete_room import DeleteRoom
from synapse_pangea_chat.delete_user import DeleteUser
from synapse_pangea_chat.direct_message import EnsureDirectMessage
from synapse_pangea_chat.direct_push import DirectPush
from synapse_pangea_chat.email_invite import CreateCourseSpace, InviteByEmail
from synapse_pangea_chat.email_policy import EmailPolicy
from synapse_pangea_chat.export_user_data import ExportUserData
from synapse_pangea_chat.find_user_by_email import FindUserByEmail
from synapse_pangea_chat.grant_instructor_analytics_access import (
    GrantInstructorAnalyticsAccess,
)
from synapse_pangea_chat.limit_user_directory import LimitUserDirectory
from synapse_pangea_chat.moderation import ChatModeration, tier1_prefilter
from synapse_pangea_chat.moderation import exempt as moderation_exempt
from synapse_pangea_chat.moderation import refusal as moderation_refusal
from synapse_pangea_chat.preview_with_code import (
    DEFAULT_PREVIEW_WITH_CODE_STATE_EVENT_TYPES,
    PreviewWithCode,
)
from synapse_pangea_chat.public_courses import PublicCourses
from synapse_pangea_chat.public_courses.backfill_l2 import PublicCoursesL2Backfill
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

logger = logging.getLogger("synapse.modules.synapse_pangea_chat")

# Every key the `moderation` config block accepts. The retired regex key is in
# the set on purpose: it has its own migration error, which says what to write
# instead, and that is a better answer than "unknown key".
_MODERATION_CONFIG_KEYS = frozenset(
    {
        "tier1_enabled",
        "tier1_phone_regions",
        "tier2_enabled",
        "choreo_base_url",
        "choreo_access_token",
        "redaction_reason_prefix",
        "tier2_workers",
        "tier2_queue_size",
        "tier2_request_timeout_seconds",
        "tier2_breaker_failure_threshold",
        "tier2_breaker_cooldown_seconds",
        "tier2_breaker_max_cooldown_seconds",
        "tier2_drain_timeout_seconds",
        "tier2_supervisor_interval_seconds",
        moderation_exempt.CONFIG_KEY,
        moderation_exempt.LEGACY_CONFIG_KEY,
        moderation_refusal.CONFIG_KEY,
    }
)


def _moderation_int(
    moderation: Dict[str, Any], key: str, low: int, high: int, default: int
) -> int:
    value = moderation.get(key, default)
    # `bool` before `int`, because `True` IS an `int` in Python and
    # `tier2_workers: true` would otherwise configure a pool of one.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'Config "moderation.{key}" must be an integer')
    if not low <= value <= high:
        raise ValueError(f'Config "moderation.{key}" must be between {low} and {high}')
    return value


def _moderation_float(
    moderation: Dict[str, Any], key: str, low: float, high: float, default: float
) -> float:
    value = moderation.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'Config "moderation.{key}" must be a number')
    if not low <= float(value) <= high:
        raise ValueError(f'Config "moderation.{key}" must be between {low} and {high}')
    return float(value)


_CHOREO_URL_SCHEMES = ("http", "https")
# What a DNS label may contain, and how long it may be. Checked because
# twisted marks a structurally invalid hostname bad and fails the connection
# before it ever resolves - so an empty interior label, a semicolon, or an
# over-long label is another value that starts cleanly and moderates nothing.
_DNS_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,63}$")


def _validate_choreo_host(hostname: Optional[str], netloc: str) -> None:
    if hostname is None:
        return
    if netloc.startswith("[") or ":" in hostname:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            raise ValueError(
                'Config "moderation.choreo_base_url" has brackets around '
                "something that is not an IP address"
            ) from None
        return
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return
    for label in hostname.rstrip(".").split("."):
        if not _DNS_LABEL.match(label):
            raise ValueError(
                'Config "moderation.choreo_base_url" host is not a usable '
                f"name: the label {label!r} is empty, too long, or contains a "
                "character a hostname cannot contain"
            )


def _validate_choreo_base_url(value: str) -> str:
    """Return the usable form of `value`, or raise saying why there is none.

    The checks are on the RAW string as well as on the parse, because the parse
    alone accepts values the request builder then mangles. The endpoint path is
    appended with `f"{base_url.rstrip('/')}/choreo/moderate"`, so a trailing `?`
    or `#` - which `urlparse` reports as an empty query and an empty fragment,
    both falsy - turns the path into `?/choreo/moderate` or swallows it into a
    fragment, and every request goes to `/`. Whitespace, control characters and
    non-ASCII fail later still, inside twisted's URI parsing, where the failure
    is one more swallowed exception per message and no error anybody sees.

    The stripped value is RETURNED rather than validated in place: validating a
    stripped copy and then storing the original is how `"https://host "` passed
    a check it did not satisfy.
    """
    raw = value.strip()
    for character, description in (("?", "a query string"), ("#", "a fragment")):
        if character in raw:
            raise ValueError(
                'Config "moderation.choreo_base_url" must not contain '
                f"{description}; the request path is appended to it, so "
                f"{character!r} would send every moderation check to a "
                "different path than the one configured"
            )
    if not raw.isascii():
        raise ValueError(
            'Config "moderation.choreo_base_url" must be ASCII. An '
            "internationalised host has to be given in its punycode form "
            '("xn--..."), because the request URI is built as bytes'
        )
    # Every space, tab, carriage return, newline and control character, not a
    # hand-written list of the ones somebody thought of: `"host\r/a"` was
    # rejected by twisted and accepted here because `\r` was not on the list.
    if any(
        character.isspace() or ord(character) < 0x21 or ord(character) == 0x7F
        for character in raw
    ):
        raise ValueError(
            'Config "moderation.choreo_base_url" must not contain whitespace '
            "or control characters; twisted refuses to build a request URI "
            "from one, on every message"
        )
    parsed = urlparse(raw)
    if parsed.scheme not in _CHOREO_URL_SCHEMES:
        raise ValueError(
            'Config "moderation.choreo_base_url" must be an http or https URL; '
            f"got scheme {parsed.scheme!r}. Every moderation check would fail "
            "against any other scheme, and each failure is swallowed by the "
            "fail-open handler, so the effect is unmoderated messages and no "
            "error."
        )
    if "@" in parsed.netloc:
        # Includes the empty-userinfo case `https://@host`, which `username`
        # and `password` both report as falsy while twisted reads the whole
        # `@host` as the hostname.
        raise ValueError(
            'Config "moderation.choreo_base_url" must not carry credentials or '
            "an empty userinfo marker; use moderation.choreo_access_token. A "
            "URL is logged and reported in far more places than a token is."
        )
    if not parsed.hostname:
        # `netloc` is not the test: `"https://:443"` has a non-empty netloc and
        # no host at all.
        raise ValueError(
            'Config "moderation.choreo_base_url" must include a host, as in '
            '"https://choreo.example.org"'
        )
    try:
        port = parsed.port
    except ValueError:
        # `urlparse` raises here for a non-numeric or out-of-range port, and
        # only when the attribute is read - which nothing did, so
        # `"https://host:99999"` and `"https://host:bad"` both started cleanly.
        raise ValueError(
            'Config "moderation.choreo_base_url" has a port that is not a '
            "number between 1 and 65535"
        ) from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(
            'Config "moderation.choreo_base_url" has a port outside 1-65535'
        )
    if parsed.netloc.endswith(":"):
        # `urlparse` reports no port for a bare trailing colon, so the range
        # check above never sees it - and twisted keeps the colon as part of
        # the hostname, which then fails to resolve on every request. Tested on
        # `netloc`, not on a bracket-stripped copy: `https://[::1]` legitimately
        # ends in a colon inside the brackets, and stripping them made a valid
        # IPv6 base URL look like a dangling port separator.
        raise ValueError(
            'Config "moderation.choreo_base_url" ends its host with a colon '
            "and no port"
        )
    _validate_choreo_host(parsed.hostname, parsed.netloc)
    if parsed.params:
        raise ValueError(
            'Config "moderation.choreo_base_url" must be a base URL with no '
            "path parameters; the request path is appended to it"
        )
    if parsed.scheme != "https":
        # Not an error: a local stack and the E2E suite legitimately run over
        # plaintext. Naming it is what keeps it a deliberate choice.
        logger.warning(
            'Config "moderation.choreo_base_url" is not https, so the '
            "moderation service account's bearer token and every moderated "
            "message cross the network in the clear"
        )
    return raw


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

        # --- Delayed Push ---
        configure_delayed_push(config)

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

        # --- Public Courses l2 backfill (one-time, operator-gated) ---
        # Off unless the operator sets public_courses_backfill_l2. Constructing
        # it is what arms it, so when the flag is false nothing is scheduled.
        self.public_courses_l2_backfill: Optional[PublicCoursesL2Backfill] = None
        if config.public_courses_backfill_l2:
            self.public_courses_l2_backfill = PublicCoursesL2Backfill(api, config)
            self.public_courses_l2_backfill.schedule()

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

        # --- Activity Session Previews ---
        # Space-scoped session discovery: a thin front on the room_preview
        # reader (shares its cache, invalidation, and rate limiter).
        self.activity_session_previews_resource = ActivitySessionPreviews(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/activity_session_previews",
            resource=self.activity_session_previews_resource,
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

        # --- Preview With Code ---
        self.preview_with_code_resource = PreviewWithCode(api, config)
        api.register_web_resource(
            path="/_synapse/client/pangea/v1/preview_with_code",
            resource=self.preview_with_code_resource,
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

        # --- Find User By Email ---
        self.find_user_by_email_resource = FindUserByEmail(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/find_user_by_email",
            resource=self.find_user_by_email_resource,
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

        # --- Email Address Policy ---
        # Homeserver-wide, so Synapse's own registration endpoint is covered
        # too, not only the Pangea route registered above.
        self.email_policy: Optional[EmailPolicy] = None
        if config.email_policy_enabled:
            self.email_policy = EmailPolicy(config, api)

        # --- Assign Room Membership ---
        self.assign_room_membership_resource = AssignRoomMembership(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/assign_room_membership",
            resource=self.assign_room_membership_resource,
        )

        # --- Grant Instructor Analytics Access ---
        self.grant_instructor_analytics_access_resource = (
            GrantInstructorAnalyticsAccess(api, config)
        )
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/grant_instructor_analytics_access",
            resource=self.grant_instructor_analytics_access_resource,
        )

        # --- Direct Message ---
        self.ensure_direct_message_resource = EnsureDirectMessage(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/ensure_direct_message",
            resource=self.ensure_direct_message_resource,
        )

        # --- Blocked Join Gate ---
        if config.blocked_join_gate_enabled:
            self.blocked_join_gate = BlockedJoinGate(config, api)

        # --- Direct Push ---
        self.direct_push_resource = DirectPush(api, config)
        self._api.register_web_resource(
            path="/_synapse/client/pangea/v1/send_push",
            resource=self.direct_push_resource,
        )

        # --- Server-side chat moderation ---
        # Constructed only when a tier is enabled: construction is what
        # registers the callbacks, so dark config stays truly dark.
        self.chat_moderation: Optional[ChatModeration] = None
        if config.moderation_tier1_enabled or config.moderation_tier2_enabled:
            self.chat_moderation = ChatModeration(api, config)

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

        public_courses_backfill_l2 = config.get("public_courses_backfill_l2", False)
        if not isinstance(public_courses_backfill_l2, bool):
            raise ValueError('Config "public_courses_backfill_l2" must be a boolean')

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

        # --- preview_with_code config ---
        preview_with_code_requests_per_burst = config.get(
            "preview_with_code_requests_per_burst", 5
        )
        if (
            not isinstance(preview_with_code_requests_per_burst, int)
            or preview_with_code_requests_per_burst < 1
        ):
            raise ValueError(
                "preview_with_code_requests_per_burst must be an integer >= 1"
            )
        preview_with_code_burst_duration_seconds = config.get(
            "preview_with_code_burst_duration_seconds", 60
        )
        if (
            not isinstance(preview_with_code_burst_duration_seconds, int)
            or preview_with_code_burst_duration_seconds < 1
        ):
            raise ValueError(
                "preview_with_code_burst_duration_seconds must be an integer >= 1"
            )
        preview_with_code_state_event_types = config.get(
            "preview_with_code_state_event_types",
            list(DEFAULT_PREVIEW_WITH_CODE_STATE_EVENT_TYPES),
        )
        if not isinstance(preview_with_code_state_event_types, list) or not all(
            isinstance(t, str) for t in preview_with_code_state_event_types
        ):
            raise ValueError(
                "preview_with_code_state_event_types must be a list of strings"
            )

        # --- delete_room config ---
        delete_room_requests_per_burst = config.get(
            "delete_room_requests_per_burst", 10
        )
        delete_room_burst_duration_seconds = config.get(
            "delete_room_burst_duration_seconds", 60
        )
        delete_room_purge_delay_seconds = config.get(
            "delete_room_purge_delay_seconds", 604800
        )

        # --- user_activity config ---
        user_activity_requests_per_burst = config.get(
            "user_activity_requests_per_burst", 10
        )
        user_activity_burst_duration_seconds = config.get(
            "user_activity_burst_duration_seconds", 60
        )
        user_activity_notification_bot_user_id = config.get(
            "user_activity_notification_bot_user_id"
        )
        if user_activity_notification_bot_user_id is not None:
            if not isinstance(user_activity_notification_bot_user_id, str):
                raise ValueError(
                    'Config "user_activity_notification_bot_user_id" must be a string'
                )
            if not user_activity_notification_bot_user_id.strip():
                raise ValueError(
                    'Config "user_activity_notification_bot_user_id" must not be empty'
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

        limit_user_directory_whitelist_candidate_user_id_patterns = config.get(
            "limit_user_directory_whitelist_candidate_user_id_patterns", []
        )
        if not isinstance(
            limit_user_directory_whitelist_candidate_user_id_patterns, list
        ):
            raise ValueError(
                'Config "limit_user_directory_whitelist_candidate_user_id_patterns" must be a list'
            )
        for pattern in limit_user_directory_whitelist_candidate_user_id_patterns:
            if not isinstance(pattern, str):
                raise ValueError(
                    'Config "limit_user_directory_whitelist_candidate_user_id_patterns" must be a list of strings'
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

        # --- find_user_by_email config ---
        find_user_by_email_requests_per_burst = config.get(
            "find_user_by_email_requests_per_burst", 10
        )
        if (
            not isinstance(find_user_by_email_requests_per_burst, int)
            or find_user_by_email_requests_per_burst < 1
        ):
            raise ValueError(
                "find_user_by_email_requests_per_burst must be an integer >= 1"
            )
        find_user_by_email_burst_duration_seconds = config.get(
            "find_user_by_email_burst_duration_seconds", 60
        )
        if (
            not isinstance(find_user_by_email_burst_duration_seconds, int)
            or find_user_by_email_burst_duration_seconds < 1
        ):
            raise ValueError(
                "find_user_by_email_burst_duration_seconds must be an integer >= 1"
            )

        # --- register_email config ---
        register_email_requests_per_burst = config.get(
            "register_email_requests_per_burst", 5
        )
        register_email_burst_duration_seconds = config.get(
            "register_email_burst_duration_seconds", 60
        )

        # --- email_policy config ---
        email_policy_enabled = config.get("email_policy_enabled", True)
        if not isinstance(email_policy_enabled, bool):
            raise ValueError("email_policy_enabled must be a boolean")

        # --- invite_by_email config ---
        invite_by_email_requests_per_burst = config.get(
            "invite_by_email_requests_per_burst", 5
        )
        invite_by_email_burst_duration_seconds = config.get(
            "invite_by_email_burst_duration_seconds", 60
        )
        app_base_url = config.get("app_base_url", "https://app.pangea.chat")

        # --- send_push config ---
        send_push_requests_per_burst = config.get("send_push_requests_per_burst", 10)
        if send_push_requests_per_burst < 1:
            raise ValueError("send_push_requests_per_burst must be >= 1")

        send_push_burst_duration_seconds = config.get(
            "send_push_burst_duration_seconds", 1
        )
        if send_push_burst_duration_seconds < 1:
            raise ValueError("send_push_burst_duration_seconds must be >= 1")

        send_push_sygnal_url = config.get("send_push_sygnal_url")
        if send_push_sygnal_url is not None:
            if not isinstance(send_push_sygnal_url, str):
                raise ValueError('Config "send_push_sygnal_url" must be a string')
            if not send_push_sygnal_url.strip():
                raise ValueError('Config "send_push_sygnal_url" must not be empty')

        # --- blocked_join_gate config ---
        blocked_join_gate_enabled = config.get("blocked_join_gate_enabled", True)
        if not isinstance(blocked_join_gate_enabled, bool):
            raise ValueError('Config "blocked_join_gate_enabled" must be a boolean')

        # --- delayed_push config ---
        delayed_push = config.get("delayed_push", {})
        if delayed_push is None:
            delayed_push = {}
        if not isinstance(delayed_push, dict):
            raise ValueError('Config "delayed_push" must be an object')

        delayed_push_enabled = delayed_push.get("enabled", False)
        if not isinstance(delayed_push_enabled, bool):
            raise ValueError('Config "delayed_push.enabled" must be a boolean')

        delayed_push_delay_ms = delayed_push.get("delay_ms", 60_000)
        if not isinstance(delayed_push_delay_ms, int) or delayed_push_delay_ms < 1:
            raise ValueError('Config "delayed_push.delay_ms" must be an integer >= 1')

        delayed_push_max_delay_ms = delayed_push.get("max_delay_ms", 600_000)
        if (
            not isinstance(delayed_push_max_delay_ms, int)
            or delayed_push_max_delay_ms < 1
        ):
            raise ValueError(
                'Config "delayed_push.max_delay_ms" must be an integer >= 1'
            )
        if delayed_push_max_delay_ms < delayed_push_delay_ms:
            raise ValueError(
                'Config "delayed_push.max_delay_ms" must be >= delayed_push.delay_ms'
            )

        delayed_push_require_synapse_version = delayed_push.get(
            "require_synapse_version", AUDITED_SYNAPSE_VERSION
        )
        if not isinstance(delayed_push_require_synapse_version, str):
            raise ValueError(
                'Config "delayed_push.require_synapse_version" must be a string'
            )
        if not delayed_push_require_synapse_version.strip():
            raise ValueError(
                'Config "delayed_push.require_synapse_version" must not be empty'
            )

        # --- moderation config ---
        moderation = config.get("moderation", {})
        if moderation is None:
            moderation = {}
        if not isinstance(moderation, dict):
            raise ValueError('Config "moderation" must be an object')

        # Unknown keys are refused, and this is a security check rather than
        # tidiness. Every key in this block is a switch that turns moderation
        # ON; ignoring one an operator misspelled means `tier1_enable: true`
        # parses cleanly, both tiers stay dark, no callback is registered and
        # nothing is logged at any level. The operator's next signal is a
        # moderation incident. `get` with a default cannot detect that - only
        # comparing the keys present against the keys that exist can.
        unknown_moderation_keys = sorted(
            str(key) for key in moderation if key not in _MODERATION_CONFIG_KEYS
        )
        if unknown_moderation_keys:
            raise ValueError(
                'Config "moderation" has unknown keys '
                f"{unknown_moderation_keys}; known keys are "
                f"{sorted(_MODERATION_CONFIG_KEYS)}"
            )

        moderation_tier1_enabled = moderation.get("tier1_enabled", False)
        if not isinstance(moderation_tier1_enabled, bool):
            raise ValueError('Config "moderation.tier1_enabled" must be a boolean')

        # Validated against libphonenumber's own region list, not just for
        # shape: every wrong value here - an empty list, "us", "US " - is a
        # string of the right type that the matcher finds no numbers for, so
        # the phone rule silently does not run. See tier1_prefilter.
        moderation_tier1_phone_regions = tier1_prefilter.validate_phone_regions(
            moderation.get("tier1_phone_regions", ["US"])
        )

        moderation_tier2_enabled = moderation.get("tier2_enabled", False)
        if not isinstance(moderation_tier2_enabled, bool):
            raise ValueError('Config "moderation.tier2_enabled" must be a boolean')

        moderation_choreo_base_url = moderation.get("choreo_base_url", None)
        moderation_choreo_access_token = moderation.get("choreo_access_token", None)
        if moderation_tier2_enabled:
            # Refuse a half-configured Tier 2 at startup rather than failing
            # (open, hence silently) on every message later.
            if (
                not isinstance(moderation_choreo_base_url, str)
                or not moderation_choreo_base_url.strip()
            ):
                raise ValueError(
                    'Config "moderation.choreo_base_url" is required when '
                    "moderation.tier2_enabled is true"
                )
            # Present is not the same as usable, and the difference is the whole
            # point of refusing a half-configured Tier 2: a non-empty string
            # that is not a URL we can fetch - "ftp://choreo.invalid",
            # "choreo.invalid" with no scheme - passes a presence check,
            # starts cleanly, and then fails on every single message inside the
            # fail-open handler, which is silence. A startup failure names the
            # problem once; the alternative names it never.
            moderation_choreo_base_url = _validate_choreo_base_url(
                moderation_choreo_base_url
            )
            if (
                not isinstance(moderation_choreo_access_token, str)
                or not moderation_choreo_access_token.strip()
            ):
                raise ValueError(
                    'Config "moderation.choreo_access_token" is required when '
                    "moderation.tier2_enabled is true"
                )

        # The retired regex key is refused rather than translated. The two
        # grammars overlap with different meanings, so any automatic
        # conversion could silently widen an exemption - and an exempt sender
        # skips both tiers. See moderation/exempt.py.
        # Presence of the key is what is refused, not its value: an operator
        # who wrote `exempt_user_id_patterns:` with nothing after it still
        # believes an exemption policy is configured, and silently accepting
        # it would leave them believing it after an upgrade changed the key.
        _ABSENT = object()
        legacy_exempt = moderation.get(moderation_exempt.LEGACY_CONFIG_KEY, _ABSENT)
        if legacy_exempt is not _ABSENT:
            raise ValueError(
                moderation_exempt.legacy_key_error(
                    [str(value) for value in legacy_exempt]
                    if isinstance(legacy_exempt, (list, tuple))
                    else []
                    if legacy_exempt is None
                    else [str(legacy_exempt)]
                )
            )

        moderation_exempt_user_id_globs = moderation.get(
            moderation_exempt.CONFIG_KEY, []
        )
        if not isinstance(moderation_exempt_user_id_globs, list) or not all(
            isinstance(pat, str) for pat in moderation_exempt_user_id_globs
        ):
            raise ValueError(
                f'Config "moderation.{moderation_exempt.CONFIG_KEY}" must be a '
                "list of strings"
            )
        for pat in moderation_exempt_user_id_globs:
            # Validated here so a bad value fails startup once instead of
            # being re-discovered on every message in the send path.
            moderation_exempt.validate_glob(pat)
            if moderation_exempt.matches_every_sender(pat):
                # Not an error: exempting everyone is a decision an operator
                # is allowed to make. Naming it is what makes it a deliberate
                # one rather than a typo nobody notices.
                logger.warning(
                    'Config "moderation.%s" entry %r exempts every sender on '
                    "every homeserver, disabling moderation for all of them",
                    moderation_exempt.CONFIG_KEY,
                    pat,
                )

        moderation_redaction_reason_prefix = moderation.get(
            "redaction_reason_prefix", "Removed by Pangea content moderation"
        )
        if (
            not isinstance(moderation_redaction_reason_prefix, str)
            or not moderation_redaction_reason_prefix.strip()
        ):
            raise ValueError(
                'Config "moderation.redaction_reason_prefix" must be a non-empty string'
            )

        # What a refused learner is told. Validated here so a misspelled rule
        # name fails startup: the default underneath an unrecognised key keeps
        # working, so at runtime an override that never took effect is
        # indistinguishable from one that did.
        moderation_tier1_refusal_messages = moderation_refusal.validate_messages(
            moderation.get(moderation_refusal.CONFIG_KEY, None)
        )

        # Bounds, not just types. Every one of these sizes a buffer, a pool or
        # a deadline, and a zero or a negative would not fail loudly - it
        # would produce a queue that accepts nothing, a pool with no workers,
        # or a deadline that has already expired, all of which look like "Tier
        # 2 is on and silently checks nothing".
        moderation_tier2_workers = _moderation_int(
            moderation, "tier2_workers", 1, 64, 8
        )
        moderation_tier2_queue_size = _moderation_int(
            moderation, "tier2_queue_size", 1, 10_000, 40
        )
        moderation_tier2_breaker_failure_threshold = _moderation_int(
            moderation, "tier2_breaker_failure_threshold", 1, 1_000, 5
        )
        moderation_tier2_request_timeout_seconds = _moderation_float(
            moderation, "tier2_request_timeout_seconds", 0.1, 120.0, 15.0
        )
        moderation_tier2_breaker_cooldown_seconds = _moderation_float(
            moderation, "tier2_breaker_cooldown_seconds", 1.0, 3_600.0, 30.0
        )
        moderation_tier2_breaker_max_cooldown_seconds = _moderation_float(
            moderation, "tier2_breaker_max_cooldown_seconds", 1.0, 86_400.0, 300.0
        )
        moderation_tier2_drain_timeout_seconds = _moderation_float(
            moderation, "tier2_drain_timeout_seconds", 0.0, 300.0, 10.0
        )
        moderation_tier2_supervisor_interval_seconds = _moderation_float(
            moderation, "tier2_supervisor_interval_seconds", 1.0, 3_600.0, 30.0
        )
        if (
            moderation_tier2_breaker_max_cooldown_seconds
            < moderation_tier2_breaker_cooldown_seconds
        ):
            raise ValueError(
                'Config "moderation.tier2_breaker_max_cooldown_seconds" must '
                "be at least moderation.tier2_breaker_cooldown_seconds; the "
                "cooldown doubles up to the maximum, so a maximum below it "
                "would shorten the first cooldown rather than cap the last"
            )

        return PangeaChatConfig(
            public_courses_burst_duration_seconds=public_courses_burst_duration_seconds,
            public_courses_requests_per_burst=public_courses_requests_per_burst,
            course_plan_state_event_type=course_plan_state_event_type,
            public_courses_cms_cache_ttl_seconds=public_courses_cms_cache_ttl_seconds,
            public_courses_backfill_l2=public_courses_backfill_l2,
            room_preview_state_event_types=all_event_types,
            room_preview_burst_duration_seconds=room_preview_burst_duration_seconds,
            room_preview_requests_per_burst=room_preview_requests_per_burst,
            knock_with_code_requests_per_burst=knock_with_code_requests_per_burst,
            knock_with_code_burst_duration_seconds=knock_with_code_burst_duration_seconds,
            preview_with_code_requests_per_burst=preview_with_code_requests_per_burst,
            preview_with_code_burst_duration_seconds=preview_with_code_burst_duration_seconds,
            preview_with_code_state_event_types=preview_with_code_state_event_types,
            delete_room_requests_per_burst=delete_room_requests_per_burst,
            delete_room_burst_duration_seconds=delete_room_burst_duration_seconds,
            delete_room_purge_delay_seconds=delete_room_purge_delay_seconds,
            user_activity_requests_per_burst=user_activity_requests_per_burst,
            user_activity_burst_duration_seconds=user_activity_burst_duration_seconds,
            user_activity_notification_bot_user_id=user_activity_notification_bot_user_id,
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
            limit_user_directory_whitelist_candidate_user_id_patterns=limit_user_directory_whitelist_candidate_user_id_patterns,
            limit_user_directory_filter_search_if_missing_public_attribute=limit_user_directory_filter_search_if_missing_public_attribute,
            user_directory_search_requests_per_burst=user_directory_search_requests_per_burst,
            user_directory_search_burst_duration_seconds=user_directory_search_burst_duration_seconds,
            find_user_by_email_requests_per_burst=find_user_by_email_requests_per_burst,
            find_user_by_email_burst_duration_seconds=find_user_by_email_burst_duration_seconds,
            register_email_requests_per_burst=register_email_requests_per_burst,
            register_email_burst_duration_seconds=register_email_burst_duration_seconds,
            email_policy_enabled=email_policy_enabled,
            invite_by_email_requests_per_burst=invite_by_email_requests_per_burst,
            invite_by_email_burst_duration_seconds=invite_by_email_burst_duration_seconds,
            app_base_url=app_base_url,
            send_push_requests_per_burst=send_push_requests_per_burst,
            send_push_burst_duration_seconds=send_push_burst_duration_seconds,
            send_push_sygnal_url=send_push_sygnal_url,
            delayed_push_enabled=delayed_push_enabled,
            delayed_push_delay_ms=delayed_push_delay_ms,
            delayed_push_max_delay_ms=delayed_push_max_delay_ms,
            delayed_push_require_synapse_version=delayed_push_require_synapse_version,
            blocked_join_gate_enabled=blocked_join_gate_enabled,
            moderation_tier1_enabled=moderation_tier1_enabled,
            moderation_tier1_phone_regions=moderation_tier1_phone_regions,
            moderation_tier2_enabled=moderation_tier2_enabled,
            moderation_choreo_base_url=moderation_choreo_base_url,
            moderation_choreo_access_token=moderation_choreo_access_token,
            moderation_exempt_user_id_globs=moderation_exempt_user_id_globs,
            moderation_redaction_reason_prefix=moderation_redaction_reason_prefix,
            moderation_tier1_refusal_messages=moderation_tier1_refusal_messages,
            moderation_tier2_workers=moderation_tier2_workers,
            moderation_tier2_queue_size=moderation_tier2_queue_size,
            moderation_tier2_request_timeout_seconds=(
                moderation_tier2_request_timeout_seconds
            ),
            moderation_tier2_breaker_failure_threshold=(
                moderation_tier2_breaker_failure_threshold
            ),
            moderation_tier2_breaker_cooldown_seconds=(
                moderation_tier2_breaker_cooldown_seconds
            ),
            moderation_tier2_breaker_max_cooldown_seconds=(
                moderation_tier2_breaker_max_cooldown_seconds
            ),
            moderation_tier2_drain_timeout_seconds=(
                moderation_tier2_drain_timeout_seconds
            ),
            moderation_tier2_supervisor_interval_seconds=(
                moderation_tier2_supervisor_interval_seconds
            ),
        )
