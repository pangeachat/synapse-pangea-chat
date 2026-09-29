"""Which Matrix user IDs are Pangea's bots and service accounts.

A naming convention, not a registry: every bot account Pangea runs has a
localpart of ``bot``, ``bot-<name>`` or ``<name>-bot``. Shared so every
endpoint that treats bots differently from people agrees on who they are.
"""

from __future__ import annotations


def is_probable_bot_user_id(user_id: str) -> bool:
    if not user_id.startswith("@") or ":" not in user_id:
        return False
    localpart = user_id[1:].split(":", 1)[0]
    return (
        localpart == "bot" or localpart.startswith("bot-") or localpart.endswith("-bot")
    )
