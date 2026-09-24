"""Shared plumbing for the nudge-delivery endpoints: secrets, URLs, the clock."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote, urlencode

from synapse.module_api import ModuleApi

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

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
    configured = getattr(config, "nudge_token_secret", None)
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
    from synapse_pangea_chat.nudge_delivery.categories import (
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
