"""Every call to action in an email this module sends is a full-width, fully
rounded button, the shape of the app's own buttons (Will, 2026-10-11). The
style is inline because many email clients drop the <style> block."""

import re
import unittest
from pathlib import Path

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
        checked = 0
        for directory in _EMAIL_TEMPLATE_DIRS:
            for path in sorted(directory.glob("*.html")):
                if path.name in _WEB_PAGES:
                    continue
                for link in _CTA_LINK.findall(path.read_text()):
                    checked += 1
                    with self.subTest(template=path.name, link=link):
                        self.assertIn("display:block;width:100%;", link)
                        self.assertIn("box-sizing:border-box;", link)
                        self.assertIn("border-radius:9999px;", link)
        # notice_email, the reminder macro and the course-ready card.
        self.assertGreaterEqual(checked, 3)

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
