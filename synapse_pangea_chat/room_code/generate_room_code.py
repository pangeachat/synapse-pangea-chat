import random

# Codes get handwritten on whiteboards and retyped by students, so the
# generation alphabet excludes every character pair that transcription
# confuses: 0/o, 1/i/l, q/g, t/y (all observed in the 2026-08-31 classroom
# burst, issue #197). Validation elsewhere still accepts the full
# [a-zA-Z0-9] set, so codes issued before this change keep working.
UNAMBIGUOUS_LETTERS = "abcdefhjkmnprsuvwxz"
UNAMBIGUOUS_DIGITS = "23456789"


def generate_access_code() -> str:
    """Generate 7 character alphanumeric access code with at least one digit,
    drawn from the unambiguous alphabet above."""

    # Ensure at least one digit by picking it explicitly
    digit = random.choice(UNAMBIGUOUS_DIGITS)

    # Generate the rest of the characters
    chars = random.choices(UNAMBIGUOUS_LETTERS + UNAMBIGUOUS_DIGITS, k=6)

    chars.append(digit)

    # Shuffle the list to randomize the position of the digit
    random.shuffle(chars)

    return "".join(chars)
