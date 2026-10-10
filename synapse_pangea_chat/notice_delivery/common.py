"""Shared plumbing for the notice-delivery endpoints: secrets, URLs, the clock."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional
from urllib.parse import quote, urlencode, urlsplit

from synapse.module_api import ModuleApi

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.notice_delivery.common")

DESTINATION_KINDS = ("app", "activity", "course", "subscription", "external")
#: The client's panel token for the subscription settings page: a
#: `settingspage` panel whose param is the `subscription` subpage
#: (client routing.instructions.md, "Reading a workspace URL").
SUBSCRIPTION_PANEL_TOKEN = "settingspage:subscription"


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

CLICK_PATH = "_synapse/client/pangea/v1/n"
UNSUBSCRIBE_PATH = "_synapse/client/pangea/v1/unsubscribe"

TOKEN_KIND_CLICK = "click"
TOKEN_KIND_UNSUBSCRIBE = "unsub"

CATEGORY_LABELS = {
    "activity_nudges": "activity reminders",
    "suggestions": "conversation and course suggestions",
    "onboarding_nudges": "getting-started tips",
    "trial_marketing": "trial and discount offers",
    "teacher_setup": "course setup reminders",
    "weekly_class_report": "the weekly class report",
    "missed_message": "missed-message emails",
    "course_invite": "course invitations",
    "campaigns": "product updates",
}


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.replace("_", " "))


def token_secret(api: ModuleApi, config: "PangeaChatConfig") -> Optional[bytes]:
    """The HMAC key for signed links: the module's own secret if configured,
    else the homeserver's macaroon secret, which every deployment already has."""
    configured = getattr(config, "notice_token_secret", None)
    if isinstance(configured, str) and configured.strip():
        return configured.encode("utf-8")
    fallback = getattr(api._hs.config.key, "macaroon_secret_key", None)
    if isinstance(fallback, bytes) and fallback:
        return fallback
    if isinstance(fallback, str) and fallback:
        return fallback.encode("utf-8")
    return None


def public_baseurl(api: ModuleApi) -> Optional[str]:
    base = getattr(api._hs.config.server, "public_baseurl", None)
    if not isinstance(base, str) or not base.strip():
        return None
    return base if base.endswith("/") else base + "/"


def now_ms(api: ModuleApi) -> int:
    return int(api._hs.get_clock().time_msec())


def click_url(base: str, token: str) -> str:
    return f"{base}{CLICK_PATH}?{urlencode({'t': token})}"


def unsubscribe_url(base: str, token: str) -> str:
    return f"{base}{UNSUBSCRIBE_PATH}?{urlencode({'t': token})}"


def app_url(
    app_base_url: str,
    *,
    activity_id: Optional[str],
    session_room_id: Optional[str],
) -> str:
    """The client's external URL contract: the shareable activity link
    (``/:activityId``, optional ``?roomid=``) or the World map root."""
    base = app_base_url.rstrip("/")
    if not activity_id:
        return f"{base}/"
    url = f"{base}/{quote(activity_id, safe='')}"
    if session_room_id:
        url += "?" + urlencode({"roomid": session_room_id})
    return url


def preference_rows(raw_preferences):
    """Display the opt-out categories with their effective persisted state."""
    from synapse_pangea_chat.notice_delivery.categories import (
        GLOBAL_OFF_CATEGORIES,
        is_refused,
        parse_preferences,
    )

    preferences = parse_preferences(raw_preferences)
    return [
        {
            "id": category,
            "label": label,
            "enabled": not is_refused(preferences, category),
        }
        for category, label in CATEGORY_LABELS.items()
        if category in GLOBAL_OFF_CATEGORIES
    ]


def external_host_allowed(url: Any, hosts: Iterable[str]) -> bool:
    """Whether ``url`` is an https link to one of the allowed ``hosts``."""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and bool(host) and host in {h.lower() for h in hosts}


def token_destination(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The compact destination a click token names. Links issued before
    destinations existed carry only the activity ids, and resolve as they
    always did."""
    compact = payload.get("d")
    if isinstance(compact, dict) and compact.get("k") in DESTINATION_KINDS:
        return compact
    if isinstance(payload.get("a"), str):
        return {"k": "activity", "a": payload["a"], "s": payload.get("s")}
    return {"k": "app"}


def destination_url(
    app_base_url: str, destination: Dict[str, Any], external_link_hosts: Iterable[str]
) -> str:
    """The URL a click on a destination lands on. The workspace URL grammar is
    the client's (routing.instructions.md): ``?c=`` is the course context and
    ``left=course`` opens its card; ``?right=`` opens a settings page."""
    base = app_base_url.rstrip("/")
    kind = destination.get("k")
    if kind == "activity":
        return app_url(
            base, activity_id=destination.get("a"), session_room_id=destination.get("s")
        )
    if kind == "course" and isinstance(destination.get("c"), str):
        return f"{base}/?{urlencode({'c': destination['c'], 'left': 'course'})}"
    if kind == "subscription":
        return f"{base}/?right={SUBSCRIPTION_PANEL_TOKEN}"
    if kind == "external":
        url = destination.get("u")
        if external_host_allowed(url, external_link_hosts):
            return str(url)
        # A host removed from the allowlist retires the links already sent:
        # the person still lands somewhere safe, and the fallback is logged.
        logger.warning(
            "notice external destination host no longer allowed; redirecting home"
        )
    return f"{base}/"
