"""Protected courses (pangeachat/.github#799): the course management event
and its rules, the record and ledger rows a Protected course's memberships
write, asking existing members, and existing-account consent at a claim.

Over the real store on an in-memory database, with Synapse's surfaces as
doubles; the routes against a real homeserver are in
``test_protected_courses_e2e.py``.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from synapse_pangea_chat.joined_via import JOINED_VIA, module_write
from synapse_pangea_chat.student_invitations.protected import (
    COURSE_MANAGEMENT,
    ManagedMembership,
    ProtectedRules,
    how_joined,
    management,
)
from synapse_pangea_chat.student_invitations.protected_api import (
    ProtectedCourseHandlers,
)
from tests.test_seat_invitations_unit import (
    INVITED,
    INVITED_KEY,
    OTHER,
    ROOM,
    STUDENT,
    TEACHER,
    THIRD,
    Harness,
)

PROTECTED = {"protected": True}
LATER = 10**14  # after any record a test writes with the real clock


def event(
    type: str,
    content: Dict[str, Any],
    *,
    state_key: Optional[str] = "",
    sender: str = TEACHER,
    event_id: str = "$e",
    replaces: Optional[str] = None,
    prev_content: Optional[Dict[str, Any]] = None,
    ts: int = 100,
    redacts: Optional[str] = None,
) -> Any:
    unsigned: Dict[str, Any] = {}
    if replaces is not None:
        unsigned["replaces_state"] = replaces
    if prev_content is not None:
        unsigned["prev_content"] = prev_content
    return SimpleNamespace(
        type=type,
        content=content,
        state_key=state_key,
        get_state_key=lambda: state_key,
        sender=sender,
        room_id=ROOM,
        event_id=event_id,
        unsigned=unsigned,
        origin_server_ts=ts,
        redacts=redacts,
    )


def member(
    user: str, membership: str, *, sender: Optional[str] = None, **kw: Any
) -> Any:
    content = {"membership": membership, **kw.pop("content", {})}
    return event("m.room.member", content, state_key=user, sender=sender or user, **kw)


def state(protected: bool) -> Dict[Tuple[str, str], Any]:
    return {(COURSE_MANAGEMENT, ""): event(COURSE_MANAGEMENT, {"protected": protected})}


class EventsMain:
    """``get_event`` over a dict of events by id, on the harness's main."""

    def __init__(self, main: Any) -> None:
        self.events: Dict[str, Any] = {}
        main.get_event = self.get_event

    async def get_event(self, event_id: str, **_: Any) -> Optional[Any]:
        return self.events.get(event_id)

    def add(self, *events: Any) -> None:
        for e in events:
            self.events[e.event_id] = e


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.h = Harness()
        self.h.api._hs.get_datastores.return_value.main = self.h.main
        self.events = EventsMain(self.h.main)
        self.store = self.h.store

    async def ledger(self) -> List[Tuple[str, str, str, str, Optional[str]]]:
        rows = await self.h.managed_events()
        return [
            (e["user_id"], e["actor"], e["action"], e["via"], e["detail"])
            for e in reversed(rows)
        ]


class TestManagementContent(unittest.TestCase):
    def test_defaults_outside_off_in_course_on_and_malformed_values_ignored(self):
        self.assertEqual(
            management({"protected": "yes", "outside": {"dms": 1, "calls": True}}),
            {
                "protected": False,
                "outside": {"other_courses": False, "dms": False, "calls": True},
                "in_course": {"other_courses": True, "dms": True, "calls": True},
            },
        )
        self.assertTrue(management(PROTECTED)["protected"])
        self.assertFalse(management(None)["protected"])


class TestRules(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.rules = ProtectedRules(self.h.api)

    async def allowed(self, e: Any, protected: bool = True) -> bool:
        ok, replacement = await self.rules.check_event_allowed(e, state(protected))
        self.assertIsNone(replacement)
        return ok

    async def test_only_the_module_writes_the_event_or_names_a_join(self):
        flag = event(COURSE_MANAGEMENT, {"protected": False})
        named = member(STUDENT, "join", content={JOINED_VIA: "claim"})
        for e in (flag, named):
            self.assertFalse(await self.allowed(e, protected=False))
            with module_write():
                self.assertTrue(await self.allowed(e, protected=False))
        self.assertTrue(await self.allowed(member(STUDENT, "join")))

    async def test_a_protected_course_keeps_knock(self):
        for rule, protected, ok in (
            ("public", True, False),
            ("invite", True, False),
            ("knock", True, True),
            ("public", False, True),
        ):
            e = event("m.room.join_rules", {"join_rule": rule, "access_code": "c"})
            self.assertEqual(await self.allowed(e, protected), ok, (rule, protected))

    async def test_the_event_cannot_be_redacted(self):
        self.events.add(
            event(COURSE_MANAGEMENT, PROTECTED, event_id="$flag"),
            event("m.room.name", {"name": "x"}, event_id="$name"),
        )
        for target, ok in (("$flag", False), ("$name", True), ("$gone", True)):
            e = event("m.room.redaction", {}, state_key=None, redacts=target)
            self.assertEqual(await self.allowed(e), ok, target)

    async def test_a_protected_course_is_never_listed(self):
        check = self.rules.check_visibility_can_be_modified
        self.assertFalse(await check(ROOM, state(True), "public"))
        self.assertTrue(await check(ROOM, state(True), "private"))
        self.assertTrue(await check(ROOM, state(False), "public"))


class TestHowJoined(_Base):
    async def test_named_join_named_invite_request_invite_and_profile_changes(self):
        main = self.h.main
        named = member(STUDENT, "join", content={JOINED_VIA: "claim"}, ts=5)
        self.assertEqual(await how_joined(main, named), ("claim", 5))
        self.events.add(
            member(STUDENT, "invite", event_id="$code", content={JOINED_VIA: "code"}),
            member(
                STUDENT, "invite", event_id="$req", prev_content={"membership": "knock"}
            ),
            member(STUDENT, "invite", event_id="$inv"),
            member(STUDENT, "join", event_id="$join", replaces="$req", ts=7),
        )
        for replaces, via in (
            ("$code", "code"),
            ("$req", "request"),
            ("$inv", "invite"),
            (None, "unknown"),
        ):
            join = member(STUDENT, "join", replaces=replaces, ts=9)
            self.assertEqual(await how_joined(main, join), (via, 9), replaces)
        # A display name change is followed back to the join it follows.
        profile = member(STUDENT, "join", replaces="$join", ts=20)
        self.assertEqual(await how_joined(main, profile), ("request", 7))


class TestJoinRecords(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.callback = ManagedMembership(
            self.h.api, self.store, self.h.claims.unmanaged
        )
        self.events.add(
            member(STUDENT, "invite", event_id="$code", content={JOINED_VIA: "code"})
        )

    async def on(self, e: Any, protected: bool = True) -> None:
        self.h.main.membership[(ROOM, e.state_key)] = e.content["membership"]
        await self.callback.on_new_event(e, state(protected))

    async def test_any_join_of_a_protected_course_manages_and_records_how(self):
        await self.on(member(STUDENT, "join", replaces="$code", ts=5))
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual(
            (record["status"], record["joined_via"], record["joined_at_ms"]),
            ("managed", "code", 5),
        )
        self.assertEqual(
            await self.ledger(),
            [(STUDENT, "system", "management_started", "code", None)],
        )

    async def test_not_protected_admins_claims_and_instructors_are_not_managed(self):
        await self.on(member(STUDENT, "join", replaces="$code"), protected=False)
        await self.on(member(TEACHER, "join", replaces="$code"))
        for user, via in ((OTHER, "claim"), (THIRD, "instructor")):
            await self.on(member(user, "join", content={JOINED_VIA: via}))
        self.h.main.server_admins.add("@ops:x")
        await self.on(member("@ops:x", "join", replaces="$code"))
        self.assertEqual(await self.store.managed_in_room(ROOM), [])
        self.assertEqual(await self.ledger(), [])

    async def test_leave_removal_ban_and_rejoin_are_ledger_rows(self):
        self.events.add(
            member(STUDENT, "join", event_id="$j", replaces="$code"),
            member(STUDENT, "leave", event_id="$l"),
            member(STUDENT, "ban", event_id="$b", sender=TEACHER),
            member(STUDENT, "invite", event_id="$i", sender=TEACHER),
        )
        await self.on(member(STUDENT, "join", replaces="$code"))
        await self.on(member(STUDENT, "join", replaces="$j"))  # a profile change
        await self.on(member(STUDENT, "leave", replaces="$j"))
        await self.on(member(STUDENT, "join", replaces="$l", ts=LATER))
        await self.on(member(STUDENT, "leave", replaces="$j", sender=TEACHER))
        await self.on(member(STUDENT, "ban", replaces="$j", sender=TEACHER))
        await self.on(member(STUDENT, "leave", replaces="$b", sender=TEACHER))
        # Turning an invitation down is no leave of the course.
        await self.on(member(STUDENT, "leave", replaces="$i"))
        self.assertEqual(
            [(actor, action) for _, actor, action, _, _ in await self.ledger()],
            [
                ("system", "management_started"),
                (STUDENT, "left"),
                (STUDENT, "rejoined"),
                (TEACHER, "removed"),
                (TEACHER, "banned"),
            ],
        )
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual(record["status"], "managed")

    async def test_a_rejoin_by_claim_is_a_rejoin_but_a_first_claim_is_not(self):
        # The claim's own record is written after its join.
        await self.store.start_managed(
            ROOM, OTHER, actor="system", via="email", joined_at_ms=1, now_ms=50
        )
        await self.on(member(OTHER, "join", content={JOINED_VIA: "claim"}, ts=40))
        self.assertEqual(len(await self.ledger()), 1)
        await self.on(member(OTHER, "join", content={JOINED_VIA: "claim"}, ts=60))
        self.assertEqual((await self.ledger())[-1][2], "rejoined")

    async def test_an_unmanaged_member_leaving_writes_nothing(self):
        self.events.add(member(OTHER, "join", event_id="$oj"))
        await self.on(member(OTHER, "leave", replaces="$oj"))
        self.assertEqual(await self.ledger(), [])


class TestAskingExistingMembers(_Base):
    async def test_asked_members_accept_or_decline_and_the_team_manages_without_asking(
        self,
    ):
        asked = await self.store.ask(
            ROOM,
            [(STUDENT, "code", 3), (OTHER, "invite", 4), (THIRD, "unknown", 5)],
            TEACHER,
            10,
        )
        self.assertEqual(asked, [STUDENT, OTHER, THIRD])
        # Asked again (a retried request): nothing changes.
        self.assertEqual(
            await self.store.ask(ROOM, [(STUDENT, "x", 1)], TEACHER, 11), []
        )
        # Controls change: no row for a member who is only asked.
        self.assertEqual(
            await self.store.record_managed(
                ROOM, "control_changed", TEACHER, 12, details=["outside.dms=on"]
            ),
            0,
        )
        answer = self.store.answer
        self.assertEqual(
            await answer(ROOM, STUDENT, accept=True, actor=STUDENT, now_ms=20),
            "managed",
        )
        self.assertEqual(
            await answer(ROOM, OTHER, accept=False, actor=OTHER, now_ms=21), "declined"
        )
        # A declined member cannot change their mind into management alone...
        self.assertIsNone(
            await answer(ROOM, OTHER, accept=True, actor=OTHER, now_ms=22)
        )
        # ...but the team, at the educator's request, manages without asking.
        self.assertEqual(
            await answer(
                ROOM,
                OTHER,
                accept=True,
                actor="@ops:x",
                now_ms=23,
                from_statuses=("asked", "declined"),
            ),
            "managed",
        )
        records = {r["user_id"]: r for r in await self.store.managed_in_room(ROOM)}
        self.assertEqual(
            {
                u: (r["status"], r["joined_via"], r["joined_at_ms"])
                for u, r in records.items()
            },
            {
                STUDENT: ("managed", "code", 3),
                OTHER: ("managed", "invite", 4),
                THIRD: ("asked", "unknown", 5),
            },
        )
        self.assertEqual(
            await self.store.record_managed(
                ROOM, "control_changed", TEACHER, 30, details=["outside.dms=on"]
            ),
            2,
        )
        self.assertEqual(
            await self.ledger(),
            [
                (STUDENT, TEACHER, "management_asked", "code", None),
                (OTHER, TEACHER, "management_asked", "invite", None),
                (THIRD, TEACHER, "management_asked", "unknown", None),
                (STUDENT, STUDENT, "management_started", "code", None),
                (OTHER, OTHER, "management_declined", "invite", None),
                (OTHER, "@ops:x", "management_started", "invite", None),
                (OTHER, TEACHER, "control_changed", "invite", "outside.dms=on"),
                (STUDENT, TEACHER, "control_changed", "code", "outside.dms=on"),
            ],
        )

    async def test_a_member_managed_by_an_earlier_claim_is_asked_too(self):
        await self.store.start_managed(
            ROOM, STUDENT, actor="system", via="email", joined_at_ms=1, now_ms=5
        )
        await self.store.start_managed(
            ROOM, OTHER, actor="system", via="code", joined_at_ms=1, now_ms=20
        )
        members = [(STUDENT, "claim", 1), (OTHER, "code", 1)]
        self.assertEqual(await self.store.ask(ROOM, members, TEACHER, 10), [STUDENT])
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual((record["status"], record["joined_via"]), ("asked", "email"))

    async def test_sign_in_and_power_level_repairs_never_overrule_an_ask(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.handlers.open(STUDENT, inv["invitation_id"])
        await self.store.ask(ROOM, [(STUDENT, "claim", 1)], TEACHER, LATER)
        await self.store.answer(ROOM, STUDENT, accept=False, actor=STUDENT, now_ms=1)
        await self.h.claims.repair_managed_for(STUDENT)
        await self.h.claims.apply_managed_rule_in_room(ROOM)
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual(record["status"], "declined")

    async def test_an_asked_member_who_joins_again_is_managed_by_that_join(self):
        await self.store.ask(ROOM, [(STUDENT, "invite", 3)], TEACHER, 10)
        await self.store.answer(ROOM, STUDENT, accept=False, actor=STUDENT, now_ms=11)
        self.assertTrue(
            await self.store.start_managed(
                ROOM, STUDENT, actor="system", via="code", joined_at_ms=40, now_ms=41
            )
        )
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual(
            (record["status"], record["joined_via"], record["joined_at_ms"]),
            ("managed", "code", 40),
        )


class TestExistingAccountConsent(_Base):
    """An account older than its invitation to a Protected course is asked
    before a claim manages it; a new one is not."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.h.admins.protected.add(ROOM)
        self.created: Dict[str, int] = {STUDENT: 1}

        async def get_user_by_id(user_id: str) -> Any:
            return SimpleNamespace(creation_ts=self.created[user_id])

        self.h.main.get_user_by_id = get_user_by_id
        self.h.verify(STUDENT, INVITED_KEY)

    async def test_open_needs_consent_then_claims_and_manages(self):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        status, body = await self.h.handlers.open(STUDENT, ident)
        self.assertEqual((status, body["result"]), (200, "needs_consent"))
        self.assertEqual((await self.h.row(ident))["state"], "invited")
        self.assertEqual(self.h.joiner.joins, [])
        # Sign-in does not claim it either.
        await self.h.claims.claim_matching_for(STUDENT)
        self.assertEqual((await self.h.row(ident))["state"], "invited")
        status, body = await self.h.handlers.open(STUDENT, ident, consent=True)
        self.assertEqual((status, body["result"]), (200, "claimed"))
        record = await self.store.managed_record(STUDENT, ROOM)
        assert record is not None
        self.assertEqual((record["status"], record["joined_via"]), ("managed", "email"))

    async def test_a_new_account_or_a_course_that_is_not_protected_claims_at_once(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        self.created[STUDENT] = 10**12  # registered after the invitation
        await self.h.claims.claim_matching_for(STUDENT)
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "joined")

    async def test_the_students_own_canvas_identity_is_not_asked(self):
        identity = ("https://canvas.example", "ctx", "u1")
        await self.store.import_canvas(
            ROOM,
            *identity[:2],
            [("u1", "c@x.example", "c@x.example")],
            TEACHER,
            LATER,
            lambda: "canvas-row",
        )
        outcome, _ = await self.h.claims.claim(
            "canvas-row", STUDENT, canvas_identity=identity
        )
        self.assertEqual(outcome, "claimed")

    async def test_another_address_is_asked_before_its_request(self):
        (inv,) = await self.h.add("someone@else.example")
        status, body = await self.h.handlers.open(STUDENT, inv["invitation_id"])
        self.assertEqual(body["result"], "needs_consent")
        self.assertIsNone(await self.store.get_request(inv["invitation_id"], STUDENT))


class FakeApi:
    """The ModuleApi surface the course management route uses."""

    def __init__(self, h: Harness) -> None:
        self._hs = h.api._hs
        self.sent: List[Tuple[Dict[str, Any], bool]] = []
        self.unlisted: List[str] = []
        self.room_state: Dict[Tuple[str, str], Any] = {
            ("m.room.join_rules", ""): event(
                "m.room.join_rules", {"join_rule": "public", "access_code": "c1"}
            ),
            ("m.room.member", TEACHER): member(TEACHER, "join"),
            ("m.room.member", STUDENT): member(STUDENT, "join", ts=3),
            ("m.room.member", OTHER): member(OTHER, "leave"),
        }
        self.public_room_list_manager = SimpleNamespace(
            remove_room_from_public_room_list=self._unlist
        )

    async def _unlist(self, room_id: str) -> None:
        self.unlisted.append(room_id)

    async def get_room_state(self, room_id: str, event_filter: Any) -> Dict:
        types = {t for t, _ in event_filter}
        return {k: v for k, v in self.room_state.items() if k[0] in types}

    async def create_and_send_event_into_room(self, d: Dict[str, Any]) -> Any:
        from synapse_pangea_chat.joined_via import is_module_write

        self.sent.append((d, is_module_write()))
        self.room_state[(d["type"], d["state_key"])] = event(d["type"], d["content"])
        return None


class TestCourseManagementRoute(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.fake = FakeApi(self.h)

    def handlers(self, enabled: bool) -> ProtectedCourseHandlers:
        return ProtectedCourseHandlers(
            self.fake,
            self.store,
            self.h.admins,
            self.h.accounts,
            self.h.claims.unmanaged,
            enabled,
        )

    async def test_dormant_nobody_can_make_a_course_protected(self):
        status, body = await self.handlers(False).set(
            TEACHER, {"room_id": ROOM, "protected": True}
        )
        self.assertEqual(
            (status, body["errcode"]), (403, "ORG.PANGEA.NOT_VERIFIED_EDUCATOR")
        )
        self.assertEqual((self.fake.sent, self.fake.unlisted), ([], []))
        status, _ = await self.handlers(True).set(
            STUDENT, {"room_id": ROOM, "protected": True}
        )
        self.assertEqual(status, 403)

    async def test_private_then_knock_then_the_flag_then_members_are_asked(self):
        h = self.handlers(True)
        status, body = await h.set(
            TEACHER, {"room_id": ROOM, "protected": True, "outside": {"dms": True}}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["asked"], [STUDENT])
        self.assertEqual(self.fake.unlisted, [ROOM])
        (rules, rules_by_module), (flag, flag_by_module) = self.fake.sent
        self.assertEqual(
            (rules["type"], rules["content"], rules["sender"]),
            ("m.room.join_rules", {"join_rule": "knock", "access_code": "c1"}, TEACHER),
        )
        self.assertEqual((flag["type"], flag_by_module), (COURSE_MANAGEMENT, True))
        self.assertEqual(management(flag["content"]), flag["content"])
        self.assertTrue(flag["content"]["outside"]["dms"])
        # No managed student yet, so the control change writes no row.
        self.assertEqual(
            await self.ledger(),
            [(STUDENT, TEACHER, "management_asked", "unknown", None)],
        )
        # A control change in the Protected course: one row per managed student.
        await self.store.answer(ROOM, STUDENT, accept=True, actor=STUDENT, now_ms=1)
        status, body = await h.set(
            TEACHER, {"room_id": ROOM, "in_course": {"calls": False}}
        )
        self.assertEqual((status, body["asked"]), (200, []))
        self.assertEqual(
            (await self.ledger())[-1],
            (STUDENT, TEACHER, "control_changed", "unknown", "in_course.calls=off"),
        )
        self.assertEqual(len(self.fake.sent), 3)  # knock is not rewritten
        status, body = await h.set(TEACHER, {"room_id": ROOM, "protected": False})
        self.assertEqual((status, body["errcode"]), (409, "ORG.PANGEA.PROTECTED_STAYS"))
        status, _ = await h.set(TEACHER, {"room_id": ROOM, "outside": {"dms": "on"}})
        self.assertEqual(status, 400)

    async def test_answer_members_and_team_routes(self):
        h = self.handlers(True)
        await h.set(TEACHER, {"room_id": ROOM, "protected": True})
        self.assertEqual(
            await h.answer(STUDENT, {"room_id": ROOM, "accept": False}),
            (200, {"status": "declined"}),
        )
        self.assertEqual(
            (await h.answer(OTHER, {"room_id": ROOM, "accept": True}))[0], 404
        )
        status, body = await h.members(TEACHER, {"room_id": ROOM})
        self.assertEqual(
            [(m["user_id"], m["status"]) for m in body["members"]],
            [(STUDENT, "declined")],
        )
        self.assertEqual((await h.members(STUDENT, {"room_id": ROOM}))[0], 403)
        args = {"room_id": ROOM, "user_ids": [STUDENT, OTHER]}
        self.assertEqual((await h.manage(TEACHER, args))[0], 403)
        self.h.main.server_admins.add("@ops:x")
        self.assertEqual(await h.manage("@ops:x", args), (200, {"managed": [STUDENT]}))


if __name__ == "__main__":
    unittest.main()
