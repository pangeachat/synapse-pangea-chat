"""Validation for caller-rendered notices. No side effects during parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from synapse_pangea_chat.config import DEFAULT_NOTICE_EXTERNAL_LINK_HOSTS
from synapse_pangea_chat.notice_delivery.categories import DELIVERABLE_CATEGORIES
from synapse_pangea_chat.notice_delivery.common import (
    DESTINATION_KINDS,
    external_host_allowed,
)
from synapse_pangea_chat.notice_delivery.eligibility import validate_eligibility

EMAIL_ONLY = frozenset({"teacher_setup", "weekly_class_report", "campaigns"})
METHODS = frozenset({"use-available", "email-only", "push-only", "in-app-only"})
LINK_SLOTS = ("{{cta_url}}", "{{unsubscribe_url}}")
SECONDARY_SLOT = "{{cta2_url}}"
#: The ids each destination kind carries; any other id on it is a mistake.
DESTINATION_IDS = {
    "app": frozenset(),
    "activity": frozenset({"activity_id", "session_room_id"}),
    "course": frozenset({"course_room_id"}),
    "subscription": frozenset(),
    "external": frozenset({"url"}),
}


def string(data: Dict[str, Any], key: str, limit: int = 512) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(
            f"{key} must be a non-empty string of at most {limit} characters"
        )
    return value.strip()


@dataclass(frozen=True)
class Destination:
    """Where a notice's action link lands: the app home, an activity (with
    its session when one exists), a course, the subscription page, or an
    allowed external page. The signed link binds it (notices.instructions.md,
    "Notice destinations")."""

    kind: str
    activity_id: Optional[str] = None
    session_room_id: Optional[str] = None
    course_room_id: Optional[str] = None
    url: Optional[str] = None

    def compact(self) -> Dict[str, str]:
        """The token payload form: ``k`` plus only the ids this kind carries."""
        result = {"k": self.kind}
        for key, value in (
            ("a", self.activity_id),
            ("s", self.session_room_id),
            ("c", self.course_room_id),
            ("u", self.url),
        ):
            if value:
                result[key] = value
        return result

    @classmethod
    def parse(
        cls,
        data: Any,
        external_link_hosts: Iterable[str],
        name: str = "destination",
    ) -> Destination:
        if not isinstance(data, dict):
            raise ValueError(f"{name} must be an object")
        kind = string(data, "kind", 32)
        if kind not in DESTINATION_KINDS:
            raise ValueError(
                f"{name}.kind must be one of {', '.join(DESTINATION_KINDS)}"
            )
        ids: Dict[str, Optional[str]] = {}
        for key, limit in (
            ("activity_id", 512),
            ("session_room_id", 512),
            ("course_room_id", 512),
            ("url", 2048),
        ):
            ids[key] = string(data, key, limit) if data.get(key) is not None else None
            if ids[key] is not None and key not in DESTINATION_IDS[kind]:
                raise ValueError(f"{name} of kind {kind} takes no {key}")
        if kind == "activity" and not ids["activity_id"]:
            raise ValueError(f"{name} of kind activity requires activity_id")
        if kind == "course" and not (ids["course_room_id"] or "").startswith("!"):
            raise ValueError(f"{name} of kind course requires a course_room_id")
        if kind == "external" and not external_host_allowed(
            ids["url"], external_link_hosts
        ):
            raise ValueError(
                f"{name} of kind external requires an https url on an allowed host"
            )
        return cls(kind, **ids)

    @classmethod
    def from_ids(cls, data: Dict[str, Any]) -> Destination:
        """The destination a request without a destination object means: its
        top-level activity (and session) ids, else the app home. This is how
        every notice resolved before destinations existed."""
        activity_id = data.get("activity_id")
        if isinstance(activity_id, str) and activity_id.strip():
            session = data.get("session_room_id")
            return cls(
                "activity",
                activity_id=activity_id.strip(),
                session_room_id=(
                    session.strip()
                    if isinstance(session, str) and session.strip()
                    else None
                ),
            )
        return cls("app")


@dataclass(frozen=True)
class EmailContent:
    subject: str
    html: str
    text: str
    receiving_reason: str

    @classmethod
    def parse(cls, data: Dict[str, Any], secondary: bool = False) -> EmailContent:
        subject = string(data, "subject", 120)
        if "\r" in subject or "\n" in subject:
            raise ValueError("email subject must be one line")
        html = string(data, "html", 384_000)
        text = string(data, "text", 128_000)
        for slot in LINK_SLOTS:
            if slot not in html or slot not in text:
                raise ValueError(f"email html and text must both contain {slot}")
        has_secondary = SECONDARY_SLOT in html and SECONDARY_SLOT in text
        if secondary and not has_secondary:
            raise ValueError(
                f"email html and text must both contain {SECONDARY_SLOT} when secondary_destination is present"
            )
        if not secondary and (SECONDARY_SLOT in html or SECONDARY_SLOT in text):
            raise ValueError(
                f"email contains {SECONDARY_SLOT} but the request has no secondary_destination"
            )

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
    log: Optional[DecisionContext]
    notification_log_id: Optional[str] = None
    """The caller's Notification_Log row. When present the caller owns the record: this module
    reserves nothing, finishes nothing, and the caller writes the receipt onto its own row."""
    destination: Destination = field(default_factory=lambda: Destination("app"))
    secondary_destination: Optional[Destination] = None

    @property
    def caller_owns_record(self) -> bool:
        return self.notification_log_id is not None

    def schedule_key(self) -> str:
        """One schedule per decision. A caller-owned record is keyed by its row
        whether or not context travels with it, because the caller already made
        the row unique per decision; otherwise by run and person, as the
        Notification_Log is."""
        if self.notification_log_id is not None:
            parts = [f"record:{self.notification_log_id}", self.user_id]
        else:
            assert self.log is not None
            parts = [self.log.run["run_id"], self.user_id]
        return json.dumps(parts, separators=(",", ":"))

    @classmethod
    def parse(
        cls,
        data: Dict[str, Any],
        external_link_hosts: Optional[Iterable[str]] = None,
    ) -> NoticeRequest:
        """``external_link_hosts`` is the configured allowlist for external
        destinations; without one, the module's default list applies."""
        validate_eligibility(data)
        hosts = (
            DEFAULT_NOTICE_EXTERNAL_LINK_HOSTS
            if external_link_hosts is None
            else external_link_hosts
        )
        for key in ("activity_id", "session_room_id"):
            if key in data and data[key] is not None:
                string(data, key)
        destination = (
            Destination.parse(data["destination"], hosts)
            if data.get("destination") is not None
            else Destination.from_ids(data)
        )
        secondary = None
        if data.get("secondary_destination") is not None:
            secondary = Destination.parse(
                data["secondary_destination"], hosts, "secondary_destination"
            )

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
            email = EmailContent.parse(data["email"], secondary=secondary is not None)

        if method in {"use-available", "push-only"} and push is None:
            raise ValueError("push content is required for this delivery method")
        if method in {"use-available", "email-only"} and email is None:
            raise ValueError("email content is required for this delivery method")
        record_id = None
        if data.get("notification_log_id") is not None:
            record_id = string(data, "notification_log_id", 128)
        log = None
        if isinstance(data.get("log"), dict):
            log = DecisionContext.parse(data["log"])
        elif record_id is None:
            raise ValueError(
                "log decision context is required unless notification_log_id names the caller's row"
            )
        if log is None and data.get("eligibility"):
            # recipient_not_returned and min_contact_spacing_ms read the decision's time and funnel.
            raise ValueError("eligibility conditions require the log decision context")
        return cls(
            user,
            category,
            variant,
            event,
            room,
            method,
            push,
            email,
            log,
            record_id,
            destination,
            secondary,
        )


def is_structured(data: Dict[str, Any]) -> bool:
    return any(
        key in data
        for key in (
            "push",
            "email",
            "delivery_method",
            "log",
            "notification_log_id",
            "scheduled_at",
            "eligibility",
        )
    )
