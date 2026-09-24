"""The two emails of a requested course's claim (knock-with-code, "Claiming a course").

1. Course ready: sent by ``create_course_space`` to the requesting address. It
   carries only the claim link, behind a button, and nothing that belongs with
   students.
2. Course claimed: sent once the admin code is used, to the requesting address
   rather than the claiming account. It carries the class link.

Both go through Synapse's own mail path (the homeserver's ``email`` config), and
the templates ship inside the package, as the nudge emails' do.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from synapse.module_api import ModuleApi

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

MAX_SUBJECT_TITLE_LENGTH = 100


def _subject_title(title: str) -> str:
    # A Subject header cannot carry line breaks.
    return " ".join(title.split())[:MAX_SUBJECT_TITLE_LENGTH]


class CourseClaimMailer:
    def __init__(self, api: ModuleApi) -> None:
        hs: Any = api._hs
        self._send_email_handler = hs.get_send_email_handler()
        self._app_name = hs.config.email.email_app_name
        [
            self._ready_html,
            self._ready_text,
            self._claimed_html,
            self._claimed_text,
        ] = api.read_templates(
            [
                "course_ready.html",
                "course_ready.txt",
                "course_claimed.html",
                "course_claimed.txt",
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
    ) -> None:
        template_vars = {
            "app_name": self._app_name,
            "course_title": course_title,
            "course_description": course_description,
            "request_summary": request_summary,
            "claim_url": claim_url,
        }
        await self._send_email_handler.send_email(
            email_address=email_address,
            subject=f"Your course is ready: {_subject_title(course_title)}",
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
    ) -> None:
        template_vars = {
            "app_name": self._app_name,
            "course_title": course_title,
            "class_url": class_url,
            "class_code": class_code,
        }
        await self._send_email_handler.send_email(
            email_address=email_address,
            subject=f"Invite your students to {_subject_title(course_title)}",
            app_name=self._app_name,
            html=self._claimed_html.render(**template_vars),
            text=self._claimed_text.render(**template_vars),
        )
