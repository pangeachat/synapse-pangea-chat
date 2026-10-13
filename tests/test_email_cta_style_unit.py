"""Every call to action in an email this module sends is a full-width, fully
rounded button, the shape of the app's own buttons (Will, 2026-10-11). The
style is inline because many email clients drop the <style> block."""

import re
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

import synapse_pangea_chat

_PACKAGE = Path(synapse_pangea_chat.__file__).parent
_EMAIL_TEMPLATE_DIRS = (
    _PACKAGE / "email_invite" / "templates",
    _PACKAGE / "notice_delivery" / "templates",
)
# Web pages, not emails: their buttons are form controls, not calls to action.
_WEB_PAGES = {
    "notice_preferences_base.html",
    "notice_link_invalid.html",
    "notice_unsubscribe_confirm.html",
    "notice_unsubscribe_done.html",
}
_CTA_LINK = re.compile(r'<a [^>]*class="cta-button"[^>]*>')


class TestEmailCtaStyle(unittest.TestCase):
    def test_every_cta_is_an_inline_full_width_pill(self) -> None:
        # The button comes from the brand email layout's macro, so check what each email renders.
        emails = {
            "notice_delivery": {
                "notice_email.html": {
                    "cta_url": "https://x.test/c",
                    "cta_label": "Open",
                }
            },
            "email_invite": {
                "course_ready.html": {
                    "claim_url": "https://x.test/c",
                    "course_title": "T",
                },
                "course_reminder.html": {
                    "claim_url": "https://x.test/c",
                    "cta_label": "Open",
                    "subject": "S",
                    "paragraphs": ["p"],
                },
            },
        }
        for folder, templates in emails.items():
            env = Environment(
                loader=FileSystemLoader(str(_PACKAGE / folder / "templates")),
                autoescape=True,
            )
            for name, values in templates.items():
                links = _CTA_LINK.findall(
                    env.get_template(name).render(app_name="Pangea Chat", **values)
                )
                with self.subTest(template=name):
                    self.assertTrue(links)
                    for link in links:
                        # display:block alone fills the column; width:100% plus padding overflowed in Gmail on iOS.
                        self.assertIn("display:block;", link)
                        self.assertNotIn("width:100%", link)
                        self.assertNotIn("box-sizing", link)
                        self.assertIn("border-radius:9999px;", link)

    def test_no_email_template_keeps_the_old_button_shape(self) -> None:
        for directory in _EMAIL_TEMPLATE_DIRS:
            for path in sorted(directory.glob("*.html")):
                if path.name in _WEB_PAGES:
                    continue
                with self.subTest(template=path.name):
                    for link in re.findall(r"<a [^>]*>", path.read_text()):
                        style = re.search(r'style="([^"]*)"', link)
                        if style and "background" in style.group(1):
                            self.assertIn("border-radius:9999px", style.group(1))


if __name__ == "__main__":
    unittest.main()
