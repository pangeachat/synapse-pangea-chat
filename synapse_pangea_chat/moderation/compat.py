"""Cross-version adapters the moderation module needs.

Existence on one pin is not compatibility: `run_as_background_process` and
`Clock` both changed shape between the two Synapse versions COMPAT.yml
requires, and calling the older shape on the newer one is a `TypeError` that
the module's own fail-open handlers swallow - Tier 2 would simply never run,
quietly, on exactly one of the two supported pins.

The same two adapters already exist, independently, in `delete_user.py`,
`export_user_data.py` and `backfill_l2.py`. This is a fourth copy and that is
a defect in its own right; the shared `synapse_pangea_chat/compat.py` that
replaces all four, with the three existing call sites migrated onto it, is a
separate and independently revertible change. It is not folded in here because
this chunk's diff is the Tier-2 transport, and moving three unrelated modules
inside it is how a transport change becomes unreviewable.
"""

import inspect
from typing import Any, Callable, Tuple

from synapse.metrics.background_process_metrics import run_as_background_process

_RUN_AS_BG_SUPPORTS_SERVER_NAME = (
    "server_name" in inspect.signature(run_as_background_process).parameters
)


def background_process_args(
    homeserver: Any, desc: str, func: Callable[..., Any]
) -> Tuple[Any, ...]:
    """The leading arguments `run_as_background_process` wants on this pin.

    Synapse 1.159 inserted `server_name` as the second positional parameter.
    Calling the 1.124 shape on 1.159 passes the coroutine function where the
    server name belongs and the first real argument where the callable
    belongs.
    """
    if _RUN_AS_BG_SUPPORTS_SERVER_NAME:
        return (desc, homeserver.hostname, func)
    return (desc, func)


class _SecondsInterval(float):
    """A seconds value every audited `Clock` contract accepts.

    Synapse 1.124's `Clock.call_later` takes plain seconds; 1.159 takes a
    `Duration` and calls `as_secs()` / `as_millis()` on it. A float subclass
    carrying both methods satisfies each without importing helpers that only
    exist on one of them. The same subclass appears in `backfill_l2.py`; see
    the module docstring for why it is duplicated rather than shared here.
    """

    def as_secs(self) -> float:
        return float(self)

    def as_millis(self) -> int:
        return int(float(self) * 1000)


class _LoopingCallInterval(int):
    """The same idea for `Clock.looping_call`, which counts in milliseconds."""

    def as_secs(self) -> float:
        return int(self) / 1000

    def as_millis(self) -> int:
        return int(self)


def looping_call_interval(interval_seconds: float) -> _LoopingCallInterval:
    return _LoopingCallInterval(int(interval_seconds * 1000))


def register_shutdown_handler(
    homeserver: Any, shutdown_func: Callable[..., Any]
) -> bool:
    """Ask the homeserver to call ``shutdown_func`` before the reactor stops.

    Returns whether a handler was registered at all, so a caller can say so
    rather than assume it.

    Two shapes. 1.159 has `register_async_shutdown_handler`, keyword-only, and
    tracks the trigger so it is removed when the homeserver is torn down -
    which matters in tests, where several homeservers come and go in one
    process. 1.124 has no such method, so the trigger goes on the reactor
    directly.

    What neither shape gives is a guarantee that the reactor WAITS for the
    handler: 1.159's wrapper launches the callback with `run_in_background`
    and returns `None`, so an async handler is fire-and-forget there. The
    drain is written to be correct under that - bounded, and counting what it
    abandons - rather than to depend on being awaited.
    """
    register = getattr(homeserver, "register_async_shutdown_handler", None)
    if register is not None:
        register(
            phase="before",
            eventType="shutdown",
            shutdown_func=shutdown_func,
        )
        return True

    reactor = getattr(homeserver, "get_reactor", None)
    if reactor is None:
        return False
    reactor().addSystemEventTrigger("before", "shutdown", shutdown_func)
    return True
