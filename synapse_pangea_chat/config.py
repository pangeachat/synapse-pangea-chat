"""
Config for the Pangea Chat module.

Unified configuration combining all previously separate synapse module configs:
- public_courses (original synapse-pangea-chat)
- room_preview (from synapse-room-preview)
- room_code (from synapse-room-code)
- delete_room (from synapse-delete-room-rest-api)
- limit_user_directory (from synapse-limit-user-directory)
"""

from typing import List, Mapping, Optional

import attr

from synapse_pangea_chat.delayed_push.delayed_push import AUDITED_SYNAPSE_VERSION


@attr.s(auto_attribs=True, frozen=True)
class PangeaChatConfig:
    """Unified config for all Pangea Chat synapse modules."""

    # --- public_courses config ---
    public_courses_burst_duration_seconds: int = 120
    public_courses_requests_per_burst: int = 120
    course_plan_state_event_type: Optional[str] = None
    public_courses_cms_cache_ttl_seconds: int = 5
    # One-time repair of existing pangea.course_plan events (add l2, normalise
    # the plan id key). Off by default: the operator turns it on, deploys,
    # watches the summary log, and turns it off again.
    public_courses_backfill_l2: bool = False

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

    # --- preview_with_code config ---
    preview_with_code_requests_per_burst: int = 5
    preview_with_code_burst_duration_seconds: int = 60
    preview_with_code_state_event_types: List[str] = attr.Factory(list)

    # --- delete_room config ---
    delete_room_requests_per_burst: int = 10
    delete_room_burst_duration_seconds: int = 60
    delete_room_purge_delay_seconds: int = 604800  # 7 days

    # --- user_activity config ---
    user_activity_requests_per_burst: int = 10
    user_activity_burst_duration_seconds: int = 60
    # Bot user ID used by the notification_cooldown_ms filter to identify bot DM rooms.
    # Required when using the notification_cooldown_ms query param.
    user_activity_notification_bot_user_id: Optional[str] = None

    # --- delete_user config ---
    delete_user_requests_per_burst: int = 5
    delete_user_burst_duration_seconds: int = 60
    delete_user_schedule_delay_seconds: int = 7 * 24 * 60 * 60
    delete_user_processor_interval_seconds: int = 60

    # --- export_user_data config ---
    export_user_data_requests_per_burst: int = 3
    export_user_data_burst_duration_seconds: int = 60
    export_user_data_processor_interval_seconds: int = 60
    export_user_data_output_dir: str = "/tmp/pangea-export-user-data"
    cms_base_url: str = ""
    cms_service_api_key: str = ""

    # --- limit_user_directory config ---
    limit_user_directory_public_attribute_search_path: Optional[str] = None
    limit_user_directory_whitelist_requester_id_patterns: List[str] = attr.Factory(list)
    limit_user_directory_whitelist_candidate_user_id_patterns: List[str] = attr.Factory(
        list
    )
    limit_user_directory_filter_search_if_missing_public_attribute: bool = True

    # --- register_email config ---
    register_email_requests_per_burst: int = 5
    register_email_burst_duration_seconds: int = 60

    # --- email_policy config ---
    # Refuse email addresses Pangea will not mail. Off switch only; the rule
    # itself is fixed by design.
    email_policy_enabled: bool = True

    # --- user_directory_search config ---
    user_directory_search_requests_per_burst: int = 10
    user_directory_search_burst_duration_seconds: int = 60

    # --- find_user_by_email config ---
    find_user_by_email_requests_per_burst: int = 10
    find_user_by_email_burst_duration_seconds: int = 60

    # --- invite_by_email config ---
    invite_by_email_requests_per_burst: int = 5
    invite_by_email_burst_duration_seconds: int = 60
    app_base_url: str = "https://app.pangea.chat"

    # --- send_push config ---
    send_push_requests_per_burst: int = 10
    send_push_burst_duration_seconds: int = 1
    send_push_sygnal_url: Optional[str] = None

    # --- delayed_push config ---
    delayed_push_enabled: bool = False
    delayed_push_delay_ms: int = 60_000
    delayed_push_max_delay_ms: int = 600_000
    delayed_push_require_synapse_version: str = AUDITED_SYNAPSE_VERSION

    # --- blocked_join_gate config ---
    # Refuse knocks/joins from users every room admin has blocked. Off switch
    # only; the rule itself is fixed by design.
    blocked_join_gate_enabled: bool = True

    # --- moderation config (server-side, trust-and-safety) ---
    # Both tiers ship dark: nothing runs until an operator enables a tier.
    moderation_tier1_enabled: bool = False
    # Regions whose NATIONAL phone formats are matched bare (international
    # +CC formats match regardless).
    moderation_tier1_phone_regions: List[str] = attr.Factory(lambda: ["US"])
    moderation_tier2_enabled: bool = False
    # Choreographer base URL (e.g. https://api.staging.pangea.chat) and the
    # Matrix access token of the moderation service account — /choreo/moderate
    # accepts any valid token on this homeserver.
    moderation_choreo_base_url: Optional[str] = None
    moderation_choreo_access_token: Optional[str] = None
    # Senders never moderated — set the bot users here: bot content is
    # already governed upstream, and Tier 2 redacting the bot's own replies
    # would fight the orchestrator. Glob patterns over the full Matrix ID
    # ('*' and '?'), matched whole-string; see moderation/exempt.py for why
    # this is not a regular expression.
    moderation_exempt_user_id_globs: List[str] = attr.Factory(list)
    moderation_redaction_reason_prefix: str = "Removed by Pangea content moderation"
    # What a learner is told when Tier 1 refuses their message, keyed by rule
    # identifier. `None` means "the built-in wording", and the wording itself
    # lives in moderation/refusal.py rather than here: importing it into this
    # file would pull the whole moderation package into an import this module
    # is upstream of. See that file for the sentences, for why each names the
    # rule and never the match, and for what an operator's override is
    # validated against at parse time.
    moderation_tier1_refusal_messages: Optional[Mapping[str, str]] = None
    # --- Tier 2 transport and concurrency ---
    # `on_new_event` is awaited inline by the notifier for every event on the
    # homeserver, so Tier 2 is a bounded queue drained by a fixed pool rather
    # than one background process per message. The defaults bound the WAIT: at
    # eight workers and a fifteen-second per-check budget, a queue of forty is
    # a worst case of about seventy-five seconds before the oldest accepted
    # message is picked up. They do not bound throughput - one instance runs
    # Tier 2, and that is the ceiling.
    moderation_tier2_workers: int = 8
    moderation_tier2_queue_size: int = 40
    # Covers the WHOLE exchange - connect, headers and body. The body half is
    # the one that had no bound at all.
    moderation_tier2_request_timeout_seconds: float = 15.0
    # Consecutive failures that open the circuit breaker, and how long it
    # stays open before admitting one probe. The cooldown doubles on a failed
    # probe up to the maximum, so a provider that is down for an hour is
    # probed a handful of times rather than a hundred.
    moderation_tier2_breaker_failure_threshold: int = 5
    moderation_tier2_breaker_cooldown_seconds: float = 30.0
    moderation_tier2_breaker_max_cooldown_seconds: float = 300.0
    # How long a clean shutdown waits for checks already in flight before
    # abandoning and counting them.
    moderation_tier2_drain_timeout_seconds: float = 10.0
    # How often the supervisor looks for a worker that died.
    moderation_tier2_supervisor_interval_seconds: float = 30.0
