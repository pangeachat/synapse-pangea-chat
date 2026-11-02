import re
import unittest
from pathlib import Path

import synapse_pangea_chat

# The first argument of a logger lookup, across line breaks: logging's own, and
# moderation's scrubbing factory every moderation logger goes through.
LOGGER_NAME = re.compile(r"\b(?:getLogger|scrubbing_logger)\(\s*([^)\s]*)")


def outside_synapse(name: str) -> bool:
    if name == "__name__":
        return True
    literal = name[1:] if name[:1] in "fr" else name
    # A name passed in a variable is checked where it is set; no argument is
    # the root logger, which the log config never disables.
    if literal[:1] not in ("'", '"'):
        return False
    return not literal[1:].startswith("synapse.")


class TestLoggerNames(unittest.TestCase):
    def test_every_module_logger_is_under_synapse(self):
        """Synapse loads modules before its log config, and dictConfig disables
        every logger that already exists outside the config's own tree, so a
        logger named after this package logs nothing, errors included."""
        root = Path(synapse_pangea_chat.__file__).parent
        outside = [
            f"{path.relative_to(root)}: {name}"
            for path in sorted(root.rglob("*.py"))
            for name in LOGGER_NAME.findall(path.read_text())
            if outside_synapse(name)
        ]
        self.assertEqual(outside, [])

    def test_the_check_catches_each_form(self):
        for name, outside in (
            ("__name__", True),
            ('"synapse_pangea_chat.x"', True),
            ("f'synapse_pangea_chat.{x}'", True),
            ('"synapse.module.synapse_pangea_chat.x"', False),
            ('f"synapse.modules.{x}"', False),
            ("_PROXY_LOGGER_NAME", False),
            ("", False),
        ):
            with self.subTest(name=name):
                self.assertEqual(outside_synapse(name), outside)
