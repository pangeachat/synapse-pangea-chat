"""The ``analytics_forward`` config block.

There is no on/off switch: the forwarder runs wherever ``url`` and ``secret``
are configured, and a deployment without the block simply has no forwarder.
Deploying admin-dash-api's ingest before configuring this is the ordering that
keeps it safe; configured early, the forwarder only backs off and its cursor
never moves, so nothing is lost.
"""

from __future__ import annotations

from typing import Any, Optional, Union
from urllib.parse import urlparse

import attr

CONFIG_KEY = "analytics_forward"

_KNOWN_KEYS = frozenset(
    {"url", "secret", "batch_max", "poll_seconds", "start_position"}
)

#: The shortest shared secret accepted. Both sides compare it in constant time,
#: so its length is the whole of its strength.
MIN_SECRET_LENGTH = 32

#: Hosts plain ``http`` may reach: the bearer secret never leaves the machine.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: ``start_position`` value meaning "the stream head when the cursor is first
#: created". Only read when no cursor is stored yet.
START_AT_CURRENT = "current"


@attr.s(auto_attribs=True, frozen=True)
class AnalyticsForwardConfig:
    #: The full URL of the ingest endpoint, ``.../api/internal/matrix-events``.
    url: str
    secret: str = attr.ib(repr=False)
    batch_max: int = 200
    poll_seconds: int = 5
    start_position: Union[str, int] = START_AT_CURRENT


def _int_in(block: dict, key: str, low: int, high: int, default: int) -> int:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'Config "{CONFIG_KEY}.{key}" must be an integer')
    if not low <= value <= high:
        raise ValueError(
            f'Config "{CONFIG_KEY}.{key}" must be between {low} and {high}'
        )
    return value


def parse_analytics_forward_config(raw: Any) -> Optional[AnalyticsForwardConfig]:
    """The parsed block, or ``None`` when no forwarder is configured."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f'Config "{CONFIG_KEY}" must be an object')
    unknown = sorted(str(key) for key in raw if key not in _KNOWN_KEYS)
    if unknown:
        raise ValueError(
            f'Config "{CONFIG_KEY}" has unknown keys {unknown}; known keys are '
            f"{sorted(_KNOWN_KEYS)}"
        )
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f'Config "{CONFIG_KEY}.url" is required')
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            f'Config "{CONFIG_KEY}.url" must be an http(s) URL with a host'
        )
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError(
            f'Config "{CONFIG_KEY}.url" must be https: the shared secret travels '
            "with every request (plain http only to this machine)"
        )
    if parsed.username or parsed.password:
        raise ValueError(
            f'Config "{CONFIG_KEY}.url" must not carry credentials; use '
            f'"{CONFIG_KEY}.secret"'
        )

    secret = raw.get("secret")
    if not isinstance(secret, str) or len(secret.strip()) < MIN_SECRET_LENGTH:
        raise ValueError(
            f'Config "{CONFIG_KEY}.secret" is required and must be '
            f"at least {MIN_SECRET_LENGTH} characters"
        )

    start = raw.get("start_position", START_AT_CURRENT)
    if start != START_AT_CURRENT and (
        isinstance(start, bool) or not isinstance(start, int) or start < 0
    ):
        raise ValueError(
            f'Config "{CONFIG_KEY}.start_position" must be "{START_AT_CURRENT}" '
            "or a stream position >= 0"
        )

    return AnalyticsForwardConfig(
        url=url,
        secret=secret.strip(),
        batch_max=_int_in(raw, "batch_max", 1, 1000, 200),
        poll_seconds=_int_in(raw, "poll_seconds", 1, 60, 5),
        start_position=start,
    )
