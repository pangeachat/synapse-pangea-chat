"""Student invitations, the claim, pending approvals and managed accounts
(CONTRACTS C2 T1-T9, S1-S3, H1-H2).

``register_student_invitations`` registers every route and the leave/kick/ban
release callback, and returns the claim service ``ClaimByEmail`` runs at
sign-in and on every verified email addition.
"""

from typing import Any, Callable, Dict, Tuple

from synapse.module_api import ModuleApi

from synapse_pangea_chat.assign_room_membership import AssignRoomMembership
from synapse_pangea_chat.notice_delivery.rate_limit import SlidingWindowRateLimiter
from synapse_pangea_chat.student_invitations.accounts import Accounts
from synapse_pangea_chat.student_invitations.api import (
    PREFIX,
    InvitationOpenRoute,
    StudentInvitationHandlers,
    StudentInvitationRoute,
    StudentInvitationsRoot,
)
from synapse_pangea_chat.student_invitations.approvals import Approvals
from synapse_pangea_chat.student_invitations.claim import StudentClaims
from synapse_pangea_chat.student_invitations.invite_email import InviteMailer
from synapse_pangea_chat.student_invitations.membership_callback import (
    MembershipRelease,
)
from synapse_pangea_chat.student_invitations.rate_limits import limit_for
from synapse_pangea_chat.student_invitations.rooms import (
    ModuleCourseAdmins,
    ModuleCourseRooms,
)
from synapse_pangea_chat.student_invitations.store import StudentInvitationStore

__all__ = ["StudentInvitations", "register_student_invitations"]


class StudentInvitations:
    def __init__(
        self,
        store: StudentInvitationStore,
        claims: StudentClaims,
        mailer: InviteMailer,
        handlers: StudentInvitationHandlers,
    ) -> None:
        self.store = store
        self.claims = claims
        self.mailer = mailer
        self.handlers = handlers


def register_student_invitations(api: ModuleApi, config: Any) -> StudentInvitations:
    homeserver = api._hs
    store = StudentInvitationStore(homeserver)
    accounts = Accounts(api)
    admins = ModuleCourseAdmins(api)
    claims = StudentClaims(store, accounts, AssignRoomMembership(api, config), admins)
    rooms = ModuleCourseRooms(api)
    mailer = InviteMailer(api, config, rooms, accounts)
    handlers = StudentInvitationHandlers(
        store=store,
        claims=claims,
        approvals=Approvals(store, claims, accounts),
        accounts=accounts,
        admins=admins,
        rooms=rooms,
        mailer=mailer,
    )
    h = handlers
    routes: Dict[str, Tuple[str, str, str, Callable[..., Any]]] = {
        # route name: (path, method, kind, handler)
        "student_invitations_add": (
            "student_invitations/add",
            "POST",
            "teacher",
            h.add,
        ),
        "student_invitations_send": (
            "student_invitations/send",
            "POST",
            "teacher",
            h.send,
        ),
        "student_invitations_list": (
            "student_invitations/list",
            "GET",
            "teacher",
            h.list,
        ),
        "student_invitations_revoke": (
            "student_invitations/revoke",
            "POST",
            "teacher",
            h.revoke,
        ),
        "student_invitations_pending_approvals": (
            "student_invitations/pending_approvals",
            "GET",
            "teacher",
            h.pending_approvals,
        ),
        "student_invitations_decide": (
            "student_invitations/decide",
            "POST",
            "teacher",
            h.decide,
        ),
        "student_invitations_approve_all": (
            "student_invitations/approve_all",
            "POST",
            "teacher",
            h.approve_all,
        ),
        "student_invitations_invite_member": (
            "student_invitations/invite_member",
            "POST",
            "teacher",
            h.invite_member,
        ),
        "student_invitations_live": (
            "student_invitations/live",
            "GET",
            "teacher",
            h.live,
        ),
        "student_invitations_events": (
            "student_invitations/events",
            "GET",
            "teacher",
            h.events,
        ),
        "student_invitations_mine_joined": (
            "student_invitations/mine/joined",
            "GET",
            "student",
            h.mine_joined,
        ),
        "student_invitations_hint": (
            "student_invitations/hint",
            "GET",
            "public",
            h.hint,
        ),
    }
    for name, (path, method, kind, handler) in routes.items():
        burst, seconds = limit_for(config.student_invitation_rate_limits, name)
        api.register_web_resource(
            path=PREFIX + path,
            resource=StudentInvitationRoute(
                homeserver,
                name,
                method,
                kind,
                handler,
                SlidingWindowRateLimiter(
                    requests_per_burst=burst, burst_duration_seconds=seconds
                ),
            ),
        )
    # S1 open: `student_invitations/{invitation_id}/open`. The fixed routes
    # above become children of this root, so only other segments reach it.
    burst, seconds = limit_for(
        config.student_invitation_rate_limits, "student_invitations_open"
    )
    api.register_web_resource(
        path=PREFIX + "student_invitations",
        resource=StudentInvitationsRoot(
            InvitationOpenRoute(
                homeserver,
                "student_invitations_open",
                "POST",
                "student",
                h.open,
                SlidingWindowRateLimiter(
                    requests_per_burst=burst, burst_duration_seconds=seconds
                ),
            )
        ),
    )
    api.register_third_party_rules_callbacks(
        on_new_event=MembershipRelease(store, claims).on_new_event
    )
    return StudentInvitations(store, claims, mailer, handlers)
