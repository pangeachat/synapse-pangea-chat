"""How a member joined, as the module's own membership writes name it
(pangeachat/.github#799).

A module path that invites or joins someone into a course puts
``org.pangea.joined_via`` in that membership event's content: ``code`` (the
class code or link), ``claim`` (an invitation claim, which records its own
management) or ``instructor`` (a path that makes the member an admin). The
Protected course rules (``student_invitations/protected.py``) refuse the key
on any event the module did not write, so a member can never name their own
way in. ``module_write`` is also how the module writes the one state event
only it may write, ``pangea.course_management``.

A leaf module: the membership paths import it without importing the
student invitations package.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Dict, Iterator

JOINED_VIA = "org.pangea.joined_via"
VIA_CODE = "code"
VIA_CLAIM = "claim"
VIA_INSTRUCTOR = "instructor"

# Set only around the module's own event writes. Contextvars follow the await
# chain into Synapse's event creation, where the rules read it, and nowhere
# else (blocked_join_gate.server_initiated_entry works the same way).
_module_write = contextvars.ContextVar("pangea_module_write", default=False)


@contextmanager
def module_write() -> Iterator[None]:
    token = _module_write.set(True)
    try:
        yield
    finally:
        _module_write.reset(token)


def is_module_write() -> bool:
    return _module_write.get()


def named(via: str) -> Dict[str, Any]:
    """The content a module membership write adds to name its path."""
    return {JOINED_VIA: via}
