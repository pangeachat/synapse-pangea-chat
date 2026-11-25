"""How a member joined, as the module's own membership writes name it
(pangeachat/.github#799): ``org.pangea.joined_via`` is ``code``, ``claim`` or
``instructor``. The Protected course rules refuse the key, and the
``pangea.course_management`` event, on any event not written under
``module_write``. A leaf module, so membership paths import it freely."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Dict, Iterator

JOINED_VIA = "org.pangea.joined_via"
VIA_CODE = "code"
VIA_CLAIM = "claim"
VIA_INSTRUCTOR = "instructor"
# On a leave the controls made (``controls.reconcile``): the refusing control.
REMOVED_BY = "org.pangea.removed_by"

# Follows the await chain into Synapse's event creation, as
# blocked_join_gate.server_initiated_entry does.
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
    return {JOINED_VIA: via}
