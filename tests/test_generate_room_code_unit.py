import unittest

from synapse_pangea_chat.room_code.generate_room_code import (
    UNAMBIGUOUS_DIGITS,
    UNAMBIGUOUS_LETTERS,
    generate_access_code,
)

# Every transcription-confusable character the generator must never emit
# (issue #197): 0/o, 1/i/l, q/g, t/y.
CONFUSABLE_CHARS = set("01iloqgty")


class TestGenerateAccessCode(unittest.TestCase):
    def test_alphabet_excludes_confusables(self) -> None:
        alphabet = set(UNAMBIGUOUS_LETTERS + UNAMBIGUOUS_DIGITS)
        self.assertFalse(alphabet & CONFUSABLE_CHARS)

    def test_generated_codes_shape_and_alphabet(self) -> None:
        allowed = set(UNAMBIGUOUS_LETTERS + UNAMBIGUOUS_DIGITS)
        for _ in range(500):
            code = generate_access_code()
            # The validation contract is unchanged: 7 chars, alphanumeric,
            # at least one digit.
            self.assertEqual(len(code), 7)
            self.assertTrue(code.isalnum())
            self.assertTrue(any(c.isdigit() for c in code))
            self.assertTrue(set(code) <= allowed, f"unexpected chars in {code!r}")


if __name__ == "__main__":
    unittest.main()
