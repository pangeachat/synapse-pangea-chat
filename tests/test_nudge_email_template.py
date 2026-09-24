import unittest

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from synapse_pangea_chat.nudge_delivery.common import TEMPLATES_DIR


class TestNudgeEmailTemplate(unittest.TestCase):
    def test_brand_shell_keeps_escaped_content_and_first_party_links(self):
        env = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR),
            autoescape=select_autoescape(),
            undefined=StrictUndefined,
        )
        html = env.get_template("nudge_email.html").render(
            app_name="Pangea Chat",
            title="Practice <today>",
            body="<script>alert(1)</script> & hello",
            cta_label="Open & practice",
            cta_url="https://matrix.example.test/n?t=abc&x=1",
            unsubscribe_url="https://matrix.example.test/unsubscribe?t=def&x=2",
            category_label="activity reminders",
            postal_address="Test address",
        )
        self.assertIn("Medium-Dark-Horizontal-Logo.png", html)
        self.assertIn("NSF.png", html)
        self.assertIn("background-color: #fdbf01", html)
        self.assertIn("Test address", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("https://matrix.example.test/n?t=abc&amp;x=1", html)
        self.assertIn("https://matrix.example.test/unsubscribe?t=def&amp;x=2", html)
        self.assertIn("Stop activity reminders", html)
        self.assertNotIn("ManageURL", html)
        self.assertNotIn("TrackView", html)
        self.assertNotIn("expressed interest", html)
