"""Validation for caller-rendered notices. No side effects during parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from synapse_pangea_chat.notice_delivery.categories import DELIVERABLE_CATEGORIES
from synapse_pangea_chat.notice_delivery.eligibility import validate_eligibility

EMAIL_ONLY = frozenset({"teacher_setup", "weekly_class_report", "campaigns"})
METHODS = frozenset({"use-available", "email-only", "push-only", "in-app-only"})
LINK_SLOTS = ("{{cta_url}}", "{{unsubscribe_url}}")


def string(data: Dict[str, Any], key: str, limit: int = 512) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(
            f"{key} must be a non-empty string of at most {limit} characters"
        )
    return value.strip()


@dataclass(frozen=True)
class EmailContent:
    subject: str
    html: str
    text: str
    receiving_reason: str

    @classmethod
    def parse(cls, data: Dict[str, Any]) -> EmailContent:
        subject = string(data, "subject", 120)
        if "\r" in subject or "\n" in subject:
            raise ValueError("email subject must be one line")
        html = string(data, "html", 384_000)
        text = string(data, "text", 128_000)
        for slot in LINK_SLOTS:
            if slot not in html or slot not in text:
                raise ValueError(f"email html and text must both contain {slot}")
        reason = string(data, "receiving_reason", 2000)
        if "{{receiving_reason}}" not in html or "{{receiving_reason}}" not in text:
            raise ValueError(
                "email html and text must both contain {{receiving_reason}}"
            )
        if "{{postal_address}}" not in html or "{{postal_address}}" not in text:
            raise ValueError("email html and text must both contain {{postal_address}}")
        return cls(subject, html, text, reason)


@dataclass(frozen=True)
class PushContent:
    title: str
    body: str
    content: Dict[str, Any]

    @classmethod
    def parse(cls, data: Dict[str, Any]) -> PushContent:
        content = data.get("content", {})
        if not isinstance(content, dict) or any(
            not k.startswith("pangea.") for k in content
        ):
            raise ValueError("push content must contain only pangea.* routing metadata")
        return cls(string(data, "title", 120), string(data, "body", 4000), content)


@dataclass(frozen=True)
class DecisionContext:
    run: Dict[str, str]
    state: Dict[str, Any]
    copy_key: str

    @classmethod
    def parse(cls, data: Dict[str, Any]) -> DecisionContext:
        run = data.get("run")
        if not isinstance(run, dict):
            raise ValueError("log.run is required")
        clean = {
            key: string(run, key)
            for key in ("run_id", "runner", "funnel", "decided_at")
        }
        if clean["runner"] not in {"bot", "skill"} or clean["funnel"] not in {
            "learner",
            "teacher",
        }:
            raise ValueError(
                "log.run requires runner bot/skill and funnel learner/teacher"
            )
        try:
            stamp = datetime.fromisoformat(clean["decided_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("log.run.decided_at must be an ISO timestamp") from error
        if stamp.tzinfo is None:
            raise ValueError("log.run.decided_at requires a timezone")
        if "policy_version" in run:
            clean["policy_version"] = string(run, "policy_version")
        state = data.get("state")
        if not isinstance(state, dict):
            raise ValueError("log.state must be an object of decision dimensions")
        return cls(clean, state, string(data, "copy_key"))


@dataclass(frozen=True)
class NoticeRequest:
    user_id: str
    category: str
    variant: str
    notice_event_id: Optional[str]
    notice_room_id: str
    method: str
    push: Optional[PushContent]
    email: Optional[EmailContent]
    log: DecisionContext

    @classmethod
    def parse(cls, data: Dict[str, Any]) -> NoticeRequest:
        validate_eligibility(data)
        user = string(data, "user_id")
        category = string(data, "category")
        variant = string(data, "variant")
        scheduled = "scheduled_at" in data
        event = None if scheduled else string(data, "notice_event_id")
        if scheduled:
            if "notice_event_id" in data:
                raise ValueError("Scheduled notices must not already have an event")
            stamp = datetime.fromisoformat(
                string(data, "scheduled_at").replace("Z", "+00:00")
            )
            if stamp.tzinfo is None:
                raise ValueError("scheduled_at requires a timezone")
            sender = string(data, "sender_id")
            if not sender.startswith("@") or ":" not in sender:
                raise ValueError("Invalid sender_id")
            content = data.get("notice_content")
            if not isinstance(content, dict) or not content:
                raise ValueError("Scheduled notices require notice_content")
            # Matrix events have a 64 KiB limit including the event envelope.
            if len(json.dumps(content).encode()) > 48_000:
                raise ValueError("notice_content is too large")
        room = string(data, "notice_room_id")
        if (
            not user.startswith("@")
            or ":" not in user
            or (event is not None and not event.startswith("$"))
            or not room.startswith("!")
        ):
            raise ValueError("Invalid Matrix user, notice event or room id")
        if category not in DELIVERABLE_CATEGORIES | {"campaigns"}:
            raise ValueError("category must be a deliverable catalog category")
        method = data.get("delivery_method", "use-available")
        if not isinstance(method, str) or method not in METHODS:
            raise ValueError("Invalid delivery_method")
        if category in EMAIL_ONLY:
            if method not in {"use-available", "email-only"}:
                raise ValueError("This category permits email only")
            method = "email-only"
        if variant == "allow_notifications":
            if category != "onboarding_nudges" or method not in {
                "use-available",
                "in-app-only",
            }:
                raise ValueError(
                    "allow_notifications is onboarding_nudges, in-app only"
                )
            method = "in-app-only"
        push = email = None
        if "push" in data:
            if not isinstance(data["push"], dict):
                raise ValueError("push must be an object")
            push = PushContent.parse(data["push"])
        if "email" in data:
            if not isinstance(data["email"], dict):
                raise ValueError("email must be an object")
            email = EmailContent.parse(data["email"])
        if method in {"use-available", "push-only"} and push is None:
            raise ValueError("push content is required for this delivery method")
        if method in {"use-available", "email-only"} and email is None:
            raise ValueError("email content is required for this delivery method")
        if not isinstance(data.get("log"), dict):
            raise ValueError("log decision context is required")
        for key in ("activity_id", "session_room_id"):
            if key in data and data[key] is not None:
                string(data, key)
        return cls(
            user,
            category,
            variant,
            event,
            room,
            method,
            push,
            email,
            DecisionContext.parse(data["log"]),
        )


def is_structured(data: Dict[str, Any]) -> bool:
    return any(
        key in data
        for key in (
            "push",
            "email",
            "delivery_method",
            "log",
            "scheduled_at",
            "eligibility",
        )
    )
