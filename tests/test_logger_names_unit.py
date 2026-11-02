import re
import unittest
from pathlib import Path

import synapse_pangea_chat

# The first argument of a getLogger call, across line breaks.
GET_LOGGER = re.compile(r"getLogger\(\s*([^)\s]*)")


class TestLoggerNames(unittest.TestCase):
    def test_every_module_logger_is_under_synapse(self):
        """Synapse loads modules before its log config, and dictConfig disables
        every logger that already exists outside the config's own tree, so a
        logger named after this package logs nothing, errors included."""
        root = Path(synapse_pangea_chat.__file__).parent
        outside = [
            f"{path.relative_to(root)}: {name}"
            for path in sorted(root.rglob("*.py"))
            for name in GET_LOGGER.findall(path.read_text())
            # A name passed in a variable is checked where it is set.
            if (name == "__name__" or name[:1] in "\"'")
            and not name.startswith(('"synapse.', "'synapse."))
        ]
        self.assertEqual(outside, [])
