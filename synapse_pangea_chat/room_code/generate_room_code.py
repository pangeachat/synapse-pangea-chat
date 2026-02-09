import random
import string


def generate_access_code() -> str:
    """Generate 7 character alphanumeric access code with at least one digit."""

    # Generate a random digit (0-9)
    digit = random.choice(string.digits)

    # Generate the rest of the characters (alphanumeric, but excluding digits for now)
    alphanumeric_chars = random.choices(string.ascii_letters + string.digits, k=6)

    # Ensure at least one number is in the code by inserting the digit at a random position
    alphanumeric_chars.append(digit)

    # Shuffle the list to randomize the position of the digit
    random.shuffle(alphanumeric_chars)

    # Convert the list to a string, make it uppercase, and return the result
    return "".join(alphanumeric_chars).lower()
