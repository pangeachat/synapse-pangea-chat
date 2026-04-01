from __future__ import annotations

from typing import Any, Dict, Optional, TypedDict


class SendPushRequest(TypedDict, total=False):
    user_id: str
    device_id: Optional[str]
    room_id: str
    event_id: str
    body: str
    title: Optional[str]
    type: Optional[str]
    content: Optional[Dict[str, Any]]
    prio: Optional[str]


class DeviceStatus(TypedDict, total=False):
    sent: bool
    app_id: str
    pushkey: str
    error: Optional[str]
    status_code: Optional[int]


class SendPushResponse(TypedDict):
    user_id: str
    attempted: int
    sent: int
    failed: int
    devices: Dict[str, DeviceStatus]
    errors: list[str]
