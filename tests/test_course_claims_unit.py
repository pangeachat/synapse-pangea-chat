"""The claim store's statements, pinned to the table they name."""

import re
import unittest

from synapse_pangea_chat.email_invite.course_claims import (
    COURSE_CLAIM_TABLE,
    STATEMENTS,
)


class TestStatements(unittest.TestCase):
    def test_every_statement_names_the_table(self) -> None:
        for sql in STATEMENTS:
            with self.subTest(sql=sql.split()[0]):
                self.assertRegex(sql, rf"\b{re.escape(COURSE_CLAIM_TABLE)}\b")

    def test_a_claim_only_takes_an_open_or_own_row(self) -> None:
        from synapse_pangea_chat.email_invite import course_claims

        self.assertIn("claimed_by IS NULL OR claimed_by = ?", course_claims._CLAIM_SQL)

    def test_sending_the_notice_clears_the_address(self) -> None:
        from synapse_pangea_chat.email_invite import course_claims

        self.assertIn("requested_email = NULL", course_claims._NOTICE_SENT_SQL)


if __name__ == "__main__":
    unittest.main()
