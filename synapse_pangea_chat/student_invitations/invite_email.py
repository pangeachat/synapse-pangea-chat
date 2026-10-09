"""The student invite email (T2): ``course_invite`` with the invitation link.

The link is the course's class link plus the invitation id,
``<app_base_url>/<class code>?inv=<invitation_id>``. Templates come from the
deployed template directory (synapse-templates), as for ``invite_by_email``.

The template's "or open Pangea from your Canvas course" line renders only when
``canvas_connected`` is true. In this release nothing is connected to Canvas,
so the module passes ``False`` without reading any LTI state; the Canvas lane
sets ``InviteMailer.canvas_connected`` to its course-link check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Protocol

from synapse_pangea_chat.email_invite.build_join_url import build_join_url

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig


class CourseRooms(Protocol):
    async def course_name(self, room_id: str) -> Optional[str]:
        ...

    async def course_topic(self, room_id: str) -> Optional[str]:
        ...


class DisplayNames(Protocol):
    async def display_name(self, user_id: str) -> Optional[str]:
        ...


class InviteMailer:
    def __init__(
        self,
        api: Any,
        config: "PangeaChatConfig",
        rooms: CourseRooms,
        names: DisplayNames,
    ):
        self._api = api
        self._config = config
        self._rooms = rooms
        self._names = names
        self._sender = api._hs.get_send_email_handler()
        self._app_name = api._hs.config.email.email_app_name
        [self._html, self._text] = api.read_templates(
            ["course_invite.html", "course_invite.txt"]
        )
        #: Release B's hook: is this course linked to a Canvas course? Unset
        #: in this release, which never reads LTI state.
        self.canvas_connected: Optional[Callable[[str], Awaitable[bool]]] = None

    async def send_invite(self, row: Dict[str, Any], access_code: str) -> None:
        room_id = row["course_room_id"]
        name = await self._rooms.course_name(room_id) or "a course"
        inviter = await self._names.display_name(row["invited_by"])
        canvas = False
        if self.canvas_connected is not None:
            canvas = await self.canvas_connected(room_id)
        variables = {
            "course_title": name,
            "course_description": await self._rooms.course_topic(room_id) or "",
            "course_avatar_url": "",
            "join_url": build_join_url(
                self._config.app_base_url, access_code, row["id"]
            ),
            "inviter_names": [inviter] if inviter else [],
            "message": "",
            "canvas_connected": canvas,
        }
        # A member invitation stores no address as entered; its key is the
        # member's own bound address.
        address = row["email"] or row["email_key"]
        await self._sender.send_email(
            email_address=address,
            subject=f"Join {name} on Pangea Chat",
            app_name=self._app_name,
            html=self._html.render(**variables),
            text=self._text.render(**variables),
        )
