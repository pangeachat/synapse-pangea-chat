"""The two emails of a requested course's claim (knock-with-code, "Claiming a course").

1. Course ready: sent by ``create_course_space`` to the requesting address. It
   carries the claim once, as a link behind a button, says the tap leads to
   sign-up or log-in, and carries nothing that belongs with students. No
   printed code and no store badges (decided 2026-10-09): a teacher who
   installs the app first claims by signing in with the requesting address.
2. Course claimed: sent once the admin code is used, to the requesting address
   rather than the claiming account. It carries the class link, and its QR code
   as an inline image for a slide.
3. Claim reminder: sent on a server admin's request to a course not yet
   claimed, with a new claim link carried as the first email carries it. The
   caller renders its words, so it does not change when the message catalog's
   templates arrive.

Both go through Synapse's own mail path (the homeserver's ``email`` config), and
the templates ship inside the package, as the notice emails' do.

Every send is bounded by ``SEND_TIMEOUT_SECONDS``. Synapse's mailer bounds the
SMTP connection but not the transaction, so a server that accepts and then
stalls would otherwise hold the caller forever: the create request without its
room id, the claim notice past its lease. The bound ends the wait; it cannot
abort a transaction Synapse's mailer has already started, so a send given up on
may still deliver later.
"""

from __future__ import annotations

import email.utils
import io
import os
import re
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as html_escape
from typing import Any, Optional

import segno
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.module_api import ModuleApi
from synapse.util.async_helpers import timeout_deferred

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

MAX_SUBJECT_TITLE_LENGTH = 100

#: How long a send may take before the caller stops waiting.
SEND_TIMEOUT_SECONDS = 120


def _subject_title(title: str) -> str:
    # A Subject header cannot carry line breaks.
    return " ".join(title.split())[:MAX_SUBJECT_TITLE_LENGTH]


def _claim_template_vars(claim_url: str, claim_code: str) -> dict[str, str]:
    # The code is accepted for the callers' sake and never rendered
    # (decided 2026-10-09): the link is the only claim path in mail.
    del claim_code
    return {"claim_url": claim_url}


CLAIM_SLOT = "{{cta_url}}"
RENDERED_SLOTS = (CLAIM_SLOT, "{{receiving_reason}}", "{{postal_address}}")
#: A pre-claim email has no account to refuse from; the request record is its refusal store.
FORBIDDEN_SLOTS = ("{{unsubscribe_url}}", "{{cta2_url}}")


def validate_rendered(html: str, text: str) -> str | None:
    """Why a caller-rendered reminder is refused, or None: both parts carry the
    claim slot and neither carries a slot this mail cannot fill."""
    for part, name in ((html, "html"), (text, "text")):
        if CLAIM_SLOT not in part:
            return f"{name} must contain {CLAIM_SLOT}"
        for slot in FORBIDDEN_SLOTS:
            if slot in part:
                return f"{name} must not contain {slot}: pre-claim mail carries no refusal link and one call to action"
    return None


CLASS_SLOT = "{{class_url}}"
CLASS_CODE_SLOT = "{{class_code}}"
#: An image source in the share kit's HTML: the QR code of the class link, attached inline.
CLASS_QR_SLOT = "{{class_qr}}"
#: The share kit goes after the claim: it carries the class link, never a claim link or a refusal link.
CLAIMED_FORBIDDEN_SLOTS = (CLAIM_SLOT, "{{unsubscribe_url}}", "{{cta2_url}}")


def validate_claimed(html: str, text: str) -> str | None:
    """Why a caller-rendered claimed email is refused, or None: both parts carry
    the class link slot, neither carries a slot this mail cannot fill, and the
    QR code is placed in the HTML only."""
    for part, name in ((html, "html"), (text, "text")):
        if CLASS_SLOT not in part:
            return f"{name} must contain {CLASS_SLOT}"
        for slot in CLAIMED_FORBIDDEN_SLOTS:
            if slot in part:
                return f"{name} must not contain {slot}: the claimed email carries the class link only"
    if CLASS_QR_SLOT in text:
        return f"text must not contain {CLASS_QR_SLOT}: plain text cannot show an image"
    return None


def fill_class_slots(
    html: str,
    text: str,
    *,
    class_url: str,
    class_code: str,
    receiving_reason: str,
    postal_address: str,
    class_qr_src: str = "",
) -> tuple[str, str]:
    """Literal replacement of the claimed email's slots, the values escaped in the HTML.
    The QR code's image source fills the HTML only."""
    values = {
        CLASS_SLOT: class_url,
        CLASS_CODE_SLOT: class_code,
        "{{receiving_reason}}": receiving_reason,
        "{{postal_address}}": postal_address,
    }
    for slot, value in values.items():
        html = html.replace(slot, html_escape(value, quote=True))
        text = text.replace(slot, value)
    html = html.replace(CLASS_QR_SLOT, html_escape(class_qr_src, quote=True))
    return html, text


class InlineImage:
    """An image attached to an email and shown in its HTML through ``src="cid:..."``."""

    def __init__(self, *, png: bytes, filename: str, id_label: str) -> None:
        self.png = png
        self.filename = filename
        # The angle brackets belong to the Content-ID header, not the reference.
        self.content_id = email.utils.make_msgid(id_label)[1:-1]

    @property
    def src(self) -> str:
        return f"cid:{self.content_id}"


def class_qr_png(class_url: str) -> bytes:
    """The QR code of the class link: about 370 px square, sharp on a projector
    when the email shows it at 200 px."""
    out = io.BytesIO()
    segno.make(class_url, error="m", micro=False).save(
        out, kind="png", scale=10, border=4
    )
    return out.getvalue()


def class_qr_image(class_url: str) -> InlineImage:
    return InlineImage(
        png=class_qr_png(class_url), filename="class-qr-code.png", id_label="class-qr"
    )


def build_related_message(
    *,
    from_string: str,
    email_address: str,
    subject: str,
    html: str,
    text: str,
    images: list[InlineImage],
) -> bytes:
    """The message Synapse's mailer would build, with ``images`` attached to the
    HTML as inline parts: the plain text, or the HTML with its images."""
    related = MIMEMultipart("related")
    related.attach(MIMEText(html, "html", "utf-8"))
    for image in images:
        part = MIMEImage(image.png, "png")
        part["Content-ID"] = f"<{image.content_id}>"
        part.add_header("Content-Disposition", "inline", filename=image.filename)
        related.attach(part)

    message = MIMEMultipart("alternative")
    # The headers Synapse's own mailer sets (synapse.handlers.send_email).
    message["Subject"] = subject
    message["From"] = from_string
    message["To"] = email_address
    message["Date"] = email.utils.formatdate()
    message["Message-ID"] = email.utils.make_msgid()
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(related)
    return message.as_string().encode("utf8")


def fill_claim_slots(
    html: str, text: str, *, claim_url: str, receiving_reason: str, postal_address: str
) -> tuple[str, str]:
    """Literal replacement of the slots, the values escaped in the HTML."""
    values = {
        CLAIM_SLOT: claim_url,
        "{{receiving_reason}}": receiving_reason,
        "{{postal_address}}": postal_address,
    }
    for slot, value in values.items():
        html = html.replace(slot, html_escape(value, quote=True))
        text = text.replace(slot, value)
    return html, text


def reminder_paragraphs(body: str) -> list[str]:
    """A reminder body's paragraphs: separated by a blank line, each with its
    own line breaks folded into spaces."""
    blocks = [" ".join(block.split()) for block in re.split(r"\n\s*\n", body)]
    return [block for block in blocks if block]


class CourseClaimMailer:
    def __init__(self, api: ModuleApi, postal_address: str = "") -> None:
        hs: Any = api._hs
        self._send_email_handler = hs.get_send_email_handler()
        self._clock = hs.get_clock()
        self._app_name = hs.config.email.email_app_name
        # The sender's postal address for a caller-rendered email's footer slot
        # (the same value notice emails carry).
        self._postal_address = postal_address
        [
            self._ready_html,
            self._ready_text,
            self._claimed_html,
            self._claimed_text,
            self._reminder_html,
            self._reminder_text,
        ] = api.read_templates(
            [
                "course_ready.html",
                "course_ready.txt",
                "course_claimed.html",
                "course_claimed.txt",
                "course_reminder.html",
                "course_reminder.txt",
            ],
            custom_template_directory=TEMPLATES_DIR,
        )

    async def send_course_ready(
        self,
        *,
        email_address: str,
        course_title: str,
        course_description: str,
        request_summary: Optional[str],
        claim_url: str,
        claim_code: str,
        claims_by_address: bool = False,
        course_image_url: str = "",
    ) -> None:
        """The canonical course_invite/teacher_invite email (engagement repo):
        the quest card with the current cover when the request carried one,
        and "Start my quest" inside it. ``claims_by_address`` is accepted for
        the callers' sake; the email no longer varies by it, because the link
        is the only claim path in mail and the re-tap line covers every
        sign-in address."""
        del claims_by_address
        template_vars = {
            "app_name": self._app_name,
            "course_title": course_title,
            "course_description": course_description,
            "request_summary": request_summary,
            "course_image_url": course_image_url,
            **_claim_template_vars(claim_url, claim_code),
        }
        await self._send(
            email_address=email_address,
            subject=f"Your quest is ready: {_subject_title(course_title)}",
            app_name=self._app_name,
            html=self._ready_html.render(**template_vars),
            text=self._ready_text.render(**template_vars),
        )

    async def send_course_claimed(
        self,
        *,
        email_address: str,
        course_title: str,
        class_url: str,
        class_code: str,
        rendered: dict[str, str] | None = None,
    ) -> None:
        """The share kit at claim: the caller's rendered email with the class link
        filled in by literal replacement when one was stored for the invitation,
        else the module's own template (invitations prepared before the caller
        rendered it). Caller content is never evaluated as a template.

        The QR code of the class link is attached inline wherever the HTML asks
        for it: always in the module's template, and in a caller's email only
        when its HTML carries the ``{{class_qr}}`` slot."""
        if rendered is not None:
            images = (
                [class_qr_image(class_url)] if CLASS_QR_SLOT in rendered["html"] else []
            )
            html, text = fill_class_slots(
                rendered["html"],
                rendered["text"],
                class_url=class_url,
                class_code=class_code,
                receiving_reason=rendered.get("receiving_reason") or "",
                postal_address=self._postal_address,
                class_qr_src=images[0].src if images else "",
            )
            await self._send(
                email_address=email_address,
                subject=_subject_title(rendered["subject"]),
                app_name=self._app_name,
                html=html,
                text=text,
                images=images,
            )
            return
        qr = class_qr_image(class_url)
        template_vars = {
            "app_name": self._app_name,
            "course_title": course_title,
            "class_url": class_url,
            "class_code": class_code,
            "class_qr_src": qr.src,
        }
        await self._send(
            email_address=email_address,
            subject=f"Invite your students to {_subject_title(course_title)}",
            app_name=self._app_name,
            html=self._claimed_html.render(**template_vars),
            text=self._claimed_text.render(**template_vars),
            images=[qr],
        )

    async def send_course_reminder(
        self,
        *,
        email_address: str,
        subject: str,
        body: str,
        cta_label: str,
        claim_url: str,
        claim_code: str,
        html: str | None = None,
        text: str | None = None,
        receiving_reason: str | None = None,
    ) -> None:
        """The reminder around the caller's body in the module's template, or,
        when the caller rendered the whole email (``html`` and ``text`` with
        the ``{{cta_url}}`` slot), that email with the claim link and the
        footer slots filled by literal replacement. Caller content is never
        evaluated as a template."""
        if html is not None and text is not None:
            rendered_html, rendered_text = fill_claim_slots(
                html,
                text,
                claim_url=claim_url,
                receiving_reason=receiving_reason or "",
                postal_address=self._postal_address,
            )
        else:
            template_vars = {
                "app_name": self._app_name,
                "subject": subject,
                "paragraphs": reminder_paragraphs(body),
                "cta_label": cta_label,
                **_claim_template_vars(claim_url, claim_code),
            }
            rendered_html = self._reminder_html.render(**template_vars)
            rendered_text = self._reminder_text.render(**template_vars)
        await self._send(
            email_address=email_address,
            subject=_subject_title(subject),
            app_name=self._app_name,
            html=rendered_html,
            text=rendered_text,
        )

    async def _send(
        self, *, images: list[InlineImage] | None = None, **kwargs: Any
    ) -> None:
        if images:
            sending = run_in_background(self._send_with_images, images=images, **kwargs)
        else:
            sending = run_in_background(self._send_email_handler.send_email, **kwargs)
        await make_deferred_yieldable(
            timeout_deferred(
                deferred=sending, timeout=SEND_TIMEOUT_SECONDS, clock=self._clock
            )
        )

    async def _send_with_images(
        self,
        *,
        email_address: str,
        subject: str,
        app_name: str,
        html: str,
        text: str,
        images: list[InlineImage],
    ) -> None:
        """Synapse's ``send_email`` sends only text and HTML, so a message with
        inline images is built here and handed to the same handler's SMTP
        transport and settings: the same sender, stream and headers. Those are
        Synapse internals, not module API; the unit tests check them against
        the Synapse version they run."""
        handler = self._send_email_handler
        try:
            from_string = handler._from % {"app": app_name}
        except (KeyError, TypeError):
            from_string = handler._from
        raw_from = email.utils.parseaddr(from_string)[1]
        raw_to = email.utils.parseaddr(email_address)[1]
        if raw_to == "":
            raise RuntimeError("Invalid 'to' address")
        message = build_related_message(
            from_string=from_string,
            email_address=email_address,
            subject=subject,
            html=html,
            text=text,
            images=images,
        )
        await handler._sendmail(
            handler._reactor,
            handler._smtp_host,
            handler._smtp_port,
            raw_from,
            raw_to,
            message,
            username=handler._smtp_user,
            password=handler._smtp_pass,
            require_auth=handler._smtp_user is not None,
            require_tls=handler._require_transport_security,
            enable_tls=handler._enable_tls,
            force_tls=handler._force_tls,
            tlsname=handler._tlsname,
        )
