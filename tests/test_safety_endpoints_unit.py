"""The Safety page's endpoints, its startup sweep and its one-time backfill.

The handlers are driven against a homeserver double whose database is real
SQLite (see `tests/moderation_doubles.py`). What a real Synapse decides - the
event handler's history visibility - is exercised end to end in
`tests/test_safety_incidents_e2e.py`; here it is a double, and the tests
assert what the handler does with each of its answers.
"""

import io
import json
import logging
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, cast
from unittest.mock import patch

from synapse.api.errors import AuthError
from synapse.http.site import SynapseRequest
from synapse.types import UserID

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.moderation.disposition import CREATE_TABLE_SQL
from synapse_pangea_chat.moderation.incidents import (
    ACTION_PRESERVED,
    ACTION_REDACTED,
    ACTION_REPORTED,
    OUTCOME_FAILED,
    OUTCOME_PENDING,
    OUTCOME_REMOVED,
    OUTCOME_UNKNOWN,
    SOURCE_REPORT,
    IncidentStore,
    new_attempt_id,
)
from synapse_pangea_chat.safety_incidents.handlers import (
    FORBIDDEN_READ,
    ReadHandler,
    ReportHandler,
)
from synapse_pangea_chat.safety_incidents.resources import (
    SafetyReport,
    SlidingWindowLimit,
)
from synapse_pangea_chat.safety_incidents.startup import (
    STATEMENTS as STARTUP_STATEMENTS,
)
from synapse_pangea_chat.safety_incidents.startup import SafetyIncidentsStartup

from .moderation_doubles import DbPoolDouble, RecordingClock
from .test_safety_incidents_unit import (
    COURSE_A,
    COURSE_B,
    OUTSIDER,
    ROOM,
    STUDENT,
    TEACHER,
    _classroom,
    _incident,
    _rows,
)

OTHER_ROOM = "!elsewhere:example.org"


class _Meta:
    def __init__(self, redacted: bool) -> None:
        self._redacted = redacted

    def is_redacted(self) -> bool:
        return self._redacted


class FakeEvent:
    def __init__(
        self,
        event_id: str = "$msg",
        room_id: str = ROOM,
        sender: str = STUDENT,
        body: str = "you are awful",
        redacted: bool = False,
    ) -> None:
        self.event_id = event_id
        self.room_id = room_id
        self.sender = sender
        self.type = "m.room.message"
        # The content is kept even when redacted, so the handler's own rule -
        # a redacted event has no text - is what a test sees, not Synapse's
        # pruning doing the work for it.
        self.content: Dict[str, Any] = {"msgtype": "m.text", "body": body}
        self.internal_metadata = _Meta(redacted)


class FakeStore:
    """The main datastore, as far as the endpoints and the startup read it."""

    def __init__(self) -> None:
        self.db_pool = DbPoolDouble()
        self.events: Dict[str, FakeEvent] = {}
        self.memberships: Dict[Tuple[str, str], str] = {}

    async def get_event(self, event_id: str, allow_none: bool = False) -> Any:
        return self.events.get(event_id)

    async def get_local_current_membership_for_user_in_room(
        self, user_id: str, room_id: str
    ) -> Tuple[Optional[str], Optional[str]]:
        return self.memberships.get((user_id, room_id)), None


class FakeEventHandler:
    """`EventHandler.get_event`: the event, wrapped as Synapse wraps it, when
    the caller may see it; `AuthError(403)` when not; None when the event is
    not in `room_id`."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.invisible_to: set = set()
        self.calls: List[Tuple[str, Optional[str], str]] = []

    async def get_event(
        self, user: UserID, room_id: Optional[str], event_id: str
    ) -> Any:
        self.calls.append((user.to_string(), room_id, event_id))
        event = self._store.events.get(event_id)
        if event is None or event.room_id != room_id:
            return None
        if user.to_string() in self.invisible_to:
            raise AuthError(403, "You don't have permission to access that event.")
        return SimpleNamespace(event=event, membership="join")


class FakeHomeServer:
    hostname = "example.org"

    def __init__(self) -> None:
        self.clock = RecordingClock()
        self.store = FakeStore()
        self.event_handler = FakeEventHandler(self.store)

    def get_clock(self) -> RecordingClock:
        return self.clock

    def get_datastores(self) -> Any:
        return SimpleNamespace(main=self.store)

    def get_event_handler(self) -> FakeEventHandler:
        return self.event_handler


def _report_body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "report_id": "4f0c2b8e-0000-4000-8000-000000000001",
        "room_id": ROOM,
        "event_id": "$msg",
        "reason": "this is bullying",
    }
    body.update(overrides)
    return body


class ReportCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.hs = FakeHomeServer()
        self.world = _classroom()
        self.store = IncidentStore(self.hs)
        self.world.rename(ROOM, "Week 3 chat")
        self.handler = ReportHandler(
            self.hs, self.store, self.world.courses(), self.world.room_names()
        )
        self.hs.store.events["$msg"] = FakeEvent()
        self.hs.store.events["$other"] = FakeEvent(
            event_id="$other", room_id=OTHER_ROOM
        )

    async def _report(self, reporter: str = TEACHER, **body: Any) -> Tuple[int, Any]:
        return await self.handler.report(
            UserID.from_string(reporter), _report_body(**body)
        )


class TestReportAdmission(ReportCase):
    async def test_an_unknown_event_is_404(self) -> None:
        status, _ = await self._report(event_id="$nope")
        self.assertEqual(status, 404)
        self.assertEqual(_rows(self.hs.store.db_pool), [])

    async def test_an_event_from_another_room_is_403(self) -> None:
        """The caller CAN see `$other` - in its own room. Naming a different
        room is what is refused: the room binding is checked, not assumed."""
        status, _ = await self._report(event_id="$other", room_id=ROOM)
        self.assertEqual(status, 403)
        self.assertEqual(_rows(self.hs.store.db_pool), [])
        self.assertEqual(
            self.hs.event_handler.calls,
            [],
            "an event from another room reached the visibility check at all",
        )

    async def test_an_event_the_caller_cannot_see_is_403(self) -> None:
        self.hs.event_handler.invisible_to.add(TEACHER)
        status, _ = await self._report()
        self.assertEqual(status, 403)
        self.assertEqual(_rows(self.hs.store.db_pool), [])

    async def test_visibility_is_asked_of_synapses_event_handler_with_the_room(
        self,
    ) -> None:
        await self._report()
        self.assertEqual(self.hs.event_handler.calls, [(TEACHER, ROOM, "$msg")])

    async def test_malformed_input_is_400(self) -> None:
        bodies: List[Dict[str, Any]] = [
            {"report_id": ""},
            {"report_id": "has spaces"},
            {"report_id": "x" * 129},
            {"report_id": 7},
            {"room_id": "not-a-room"},
            {"event_id": "no-dollar"},
            {"reason": ["a list"]},
        ]
        for body in bodies:
            status, _ = await self._report(**body)
            self.assertEqual(status, 400, body)
        status, _ = await self.handler.report(UserID.from_string(TEACHER), ["x"])
        self.assertEqual(status, 400)


class TestReportSnapshot(ReportCase):
    async def test_a_report_snapshots_sender_text_reason_and_the_subjects_course(
        self,
    ) -> None:
        status, payload = await self._report()
        self.assertEqual(status, 200)
        incident_id = "report:4f0c2b8e-0000-4000-8000-000000000001"
        self.assertEqual(payload, {"incident_id": incident_id})
        row = await self.store.get(incident_id)
        assert row is not None
        self.assertEqual(row.source, SOURCE_REPORT)
        self.assertEqual(row.action, ACTION_REPORTED)
        self.assertIsNone(row.outcome)
        self.assertEqual(row.subject_id, STUDENT)
        self.assertEqual(row.reporter_id, TEACHER)
        self.assertEqual((row.room_id, row.event_id), (ROOM, "$msg"))
        self.assertEqual(row.text, "you are awful")
        self.assertEqual(row.reason, "this is bullying")
        self.assertEqual(row.room_name, "Week 3 chat")
        self.assertEqual(
            row.course_ids,
            (COURSE_A,),
            "a teacher's report reached a course other than the learner's",
        )

    async def test_a_retry_with_the_same_id_is_idempotent(self) -> None:
        first = await self._report()
        self.hs.store.events["$msg"] = FakeEvent(body="edited since")
        second = await self._report(reason="a second reason")
        self.assertEqual(first, second)
        rows = _rows(self.hs.store.db_pool)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][6], "you are awful")
        self.assertEqual(rows[0][7], "this is bullying")

    async def test_an_id_already_used_by_another_report_is_not_a_silent_200(
        self,
    ) -> None:
        await self._report()
        status, _ = await self._report(reporter=OUTSIDER)
        self.assertEqual(status, 409)

    async def test_a_redacted_event_is_recorded_with_no_text(self) -> None:
        self.hs.store.events["$msg"] = FakeEvent(redacted=True)
        status, payload = await self._report()
        self.assertEqual(status, 200)
        row = await self.store.get(payload["incident_id"])
        assert row is not None
        self.assertIsNone(row.text)
        self.assertEqual(row.reason, "this is bullying")

    async def test_nul_in_the_reason_and_the_text(self) -> None:
        self.hs.store.events["$msg"] = FakeEvent(body="a\x00b")
        status, payload = await self._report(reason="r\x00s")
        self.assertEqual(status, 200)
        row = await self.store.get(payload["incident_id"])
        assert row is not None
        self.assertEqual((row.text, row.reason), ("a␀b", "r␀s"))

    async def test_a_report_with_no_reason_is_recorded(self) -> None:
        status, payload = await self._report(reason=None)
        self.assertEqual(status, 200)
        row = await self.store.get(payload["incident_id"])
        assert row is not None
        self.assertIsNone(row.reason)

    async def test_a_course_lookup_failure_writes_nothing(self) -> None:
        self.world.error = RuntimeError("state unreadable")
        with self.assertRaises(RuntimeError):
            await self._report()
        self.assertEqual(_rows(self.hs.store.db_pool), [])


# ---------------------------------------------------------------------------
# The read endpoint
# ---------------------------------------------------------------------------


class TestReadIsCourseAdminsOnly(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.hs = FakeHomeServer()
        self.world = _classroom()
        self.store = IncidentStore(self.hs)
        self.handler = ReadHandler(self.hs, self.store)
        self.handler._read_state = self.world.read_state
        for user, rooms in self.world.joined.items():
            for room in rooms:
                self.hs.store.memberships[(user, room)] = "join"

    async def _seed(self) -> None:
        await self.store.insert(_incident(incident_id="mod:$2", created_ms=20))
        await self.store.insert(_incident(incident_id="mod:$1", created_ms=10))
        await self.store.insert(
            _incident(incident_id="mod:$b", course_ids=(COURSE_B,), created_ms=5)
        )

    async def test_a_course_admin_reads_every_row_of_their_course_in_order(
        self,
    ) -> None:
        await self._seed()
        status, payload = await self.handler.read(TEACHER, COURSE_A)
        self.assertEqual(status, 200)
        incidents = payload["incidents"]
        self.assertEqual([i["incident_id"] for i in incidents], ["mod:$1", "mod:$2"])
        self.assertEqual(
            set(incidents[0]),
            {
                "incident_id",
                "source",
                "action",
                "outcome",
                "subject_id",
                "reporter_id",
                "room_id",
                "room_name",
                "event_id",
                "categories",
                "self_harm",
                "rule",
                "top_score",
                "text",
                "reason",
                "created_ms",
                "updated_ms",
            },
            "the response is not the contract's Incident",
        )

    async def test_a_student_is_refused(self) -> None:
        await self._seed()
        self.assertEqual(
            await self.handler.read(STUDENT, COURSE_A), (403, FORBIDDEN_READ)
        )

    async def test_a_departed_admin_is_refused(self) -> None:
        await self._seed()
        self.hs.store.memberships[(TEACHER, COURSE_A)] = "leave"
        self.world.leave(TEACHER, COURSE_A)
        self.assertEqual(
            await self.handler.read(TEACHER, COURSE_A), (403, FORBIDDEN_READ)
        )

    async def test_a_departed_admin_is_refused_on_membership_alone(self) -> None:
        """Membership is read first, from the local membership table, and a
        caller who has left is refused even if a stale state read said
        otherwise."""
        await self._seed()
        self.hs.store.memberships[(TEACHER, COURSE_A)] = "leave"
        self.assertEqual(
            await self.handler.read(TEACHER, COURSE_A), (403, FORBIDDEN_READ)
        )

    async def test_a_room_that_is_not_a_course_is_refused(self) -> None:
        self.world.room(ROOM, course=False, teacher=100)
        await self.store.insert(_incident(incident_id="mod:$r", course_ids=(ROOM,)))
        self.assertEqual(await self.handler.read(TEACHER, ROOM), (403, FORBIDDEN_READ))

    async def test_a_bad_space_id_is_400(self) -> None:
        for value in (None, "", "not-a-room", 7):
            status, _ = await self.handler.read(TEACHER, value)
            self.assertEqual(status, 400, value)


class TestRateLimit(unittest.TestCase):
    def test_sliding_window(self) -> None:
        limit = SlidingWindowLimit(2, 60)
        self.assertFalse(limit.is_limited("@a:x", now=0))
        self.assertFalse(limit.is_limited("@a:x", now=1))
        self.assertTrue(limit.is_limited("@a:x", now=2))
        self.assertFalse(limit.is_limited("@b:x", now=2), "callers share a budget")
        self.assertFalse(limit.is_limited("@a:x", now=62))


class TestConfig(unittest.TestCase):
    def test_defaults_and_validation(self) -> None:
        required = {"cms_base_url": "http://cms.invalid", "cms_service_api_key": "k"}
        config = PangeaChat.parse_config(dict(required))
        self.assertEqual(config.safety_report_requests_per_burst, 30)
        self.assertEqual(config.safety_incidents_requests_per_burst, 60)
        for key in (
            "safety_report_requests_per_burst",
            "safety_report_burst_duration_seconds",
            "safety_incidents_requests_per_burst",
            "safety_incidents_burst_duration_seconds",
        ):
            for bad in (0, -1, True, "5"):
                with self.assertRaises(ValueError, msg=f"{key}={bad!r}"):
                    PangeaChat.parse_config({**required, key: bad})


# ---------------------------------------------------------------------------
# What reaches a log, on the report path
# ---------------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestReportNeverLogsTextOrReason(ReportCase):
    SECRET_TEXT = "okapi-text-9911"
    SECRET_REASON = "okapi-reason-9922"

    async def _render(self, body: bytes) -> List[Tuple[int, Any]]:
        responses: List[Tuple[int, Any]] = []
        requester = SimpleNamespace(user=UserID.from_string(TEACHER))

        async def _auth(request: Any) -> Any:
            return requester

        homeserver = SimpleNamespace(
            get_auth=lambda: SimpleNamespace(get_user_by_req=_auth)
        )
        resource = SafetyReport(homeserver, self.handler, SlidingWindowLimit(100, 60))
        request = SimpleNamespace(content=io.BytesIO(body), args={})

        def _respond(_request: Any, status: int, payload: Any, send_cors: bool) -> None:
            responses.append((status, payload))

        with patch(
            "synapse_pangea_chat.safety_incidents.resources.respond_with_json", _respond
        ):
            await resource._render(cast(SynapseRequest, request))
        return responses

    async def test_success_failure_and_a_malformed_body_log_neither(self) -> None:
        capture = _Capture()
        root = logging.getLogger()
        root.addHandler(capture)
        old = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, capture)
        self.addCleanup(root.setLevel, old)

        self.hs.store.events["$msg"] = FakeEvent(body=self.SECRET_TEXT)
        body = json.dumps(_report_body(reason=self.SECRET_REASON)).encode()
        self.assertEqual((await self._render(body))[0][0], 200)

        self.hs.store.db_pool.error = RuntimeError(
            f"driver quoting {self.SECRET_TEXT} {self.SECRET_REASON}"
        )
        body = json.dumps(
            _report_body(report_id="second", reason=self.SECRET_REASON)
        ).encode()
        self.assertEqual((await self._render(body))[0][0], 500)

        broken = b'{"reason": "' + self.SECRET_REASON.encode() + b'"'
        self.assertEqual((await self._render(broken))[0][0], 400)

        self.assertTrue(capture.records, "nothing was logged, so nothing was checked")
        for record in capture.records:
            rendered = " ".join([record.getMessage(), repr(vars(record))])
            self.assertNotIn(self.SECRET_TEXT, rendered)
            self.assertNotIn(self.SECRET_REASON, rendered)


# ---------------------------------------------------------------------------
# Startup: the sweep and the backfill
# ---------------------------------------------------------------------------


class StartupCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.hs = FakeHomeServer()
        self.world = _classroom()
        self.store = IncidentStore(self.hs)

    def _startup(self) -> SafetyIncidentsStartup:
        return SafetyIncidentsStartup(
            self.hs, self.store, self.world.courses(), self.world.room_names()
        )


class TestStartupSweep(StartupCase):
    async def _pending(self, name: str, attempt_id: Optional[str]) -> None:
        await self.store.insert(
            _incident(
                incident_id=f"mod:${name}",
                event_id=f"${name}",
                action=ACTION_REDACTED,
                outcome=OUTCOME_PENDING,
                attempt_id=attempt_id,
            )
        )

    async def _outcome_of(self, name: str) -> Optional[str]:
        row = await self.store.get(f"mod:${name}")
        assert row is not None
        return row.outcome

    async def test_pending_rows_left_by_another_process_are_settled(self) -> None:
        await self._pending("gone", "crashed-process:1")
        await self._pending("up", "crashed-process:2")
        await self._pending("missing", None)
        await self._pending("live", new_attempt_id())
        await self.store.insert(
            _incident(
                incident_id="mod:$failed", event_id="$failed", outcome=OUTCOME_FAILED
            )
        )
        self.hs.store.events["$gone"] = FakeEvent(event_id="$gone", redacted=True)
        self.hs.store.events["$up"] = FakeEvent(event_id="$up")
        self.assertEqual(await self._startup().sweep(), 3)
        self.assertEqual(await self._outcome_of("gone"), OUTCOME_REMOVED)
        self.assertEqual(await self._outcome_of("up"), OUTCOME_UNKNOWN)
        self.assertEqual(await self._outcome_of("missing"), OUTCOME_UNKNOWN)
        self.assertEqual(
            await self._outcome_of("live"),
            OUTCOME_PENDING,
            "the sweep settled an attempt this process is still running",
        )
        self.assertEqual(await self._outcome_of("failed"), OUTCOME_FAILED)

    async def test_an_ordinary_update_does_not_hide_a_crashed_attempt(self) -> None:
        """A preserve verdict after boot rewrites the row - action, categories,
        `updated_ms` - without starting an attempt. The crashed attempt it
        carries is still the one the sweep must settle."""
        await self._pending("crashed", "crashed-process:1")
        await self.store.upsert_verdict(
            _incident(
                incident_id="mod:$crashed",
                event_id="$crashed",
                action=ACTION_PRESERVED,
                categories=("self_harm",),
                self_harm=True,
            )
        )
        self.assertEqual(await self._startup().sweep(), 1)
        self.assertEqual(await self._outcome_of("crashed"), OUTCOME_UNKNOWN)


class TestTheSweepNeverTouchesANewAttempt(StartupCase):
    async def test_a_row_taken_over_after_it_was_read_is_left_alone(self) -> None:
        """The sweep reads its list, and a new attempt in this process takes
        the row over before the sweep reaches it. The settle is scoped to the
        attempt the list named, so the live attempt keeps the row."""
        await self.store.insert(
            _incident(
                incident_id="mod:$live",
                event_id="$live",
                outcome=OUTCOME_PENDING,
                attempt_id=new_attempt_id(),
            )
        )

        async def _stale_list() -> List[Tuple[str, Optional[str], Optional[str]]]:
            return [("mod:$live", "$live", "crashed-process:1")]

        with patch.object(self.store, "pending", _stale_list):
            self.assertEqual(await self._startup().sweep(), 0)
        row = await self.store.get("mod:$live")
        assert row is not None
        self.assertEqual(row.outcome, OUTCOME_PENDING)


_SYNAPSE_TABLES = (
    "CREATE TABLE events (event_id TEXT, room_id TEXT, sender TEXT, type TEXT, "
    "stream_ordering BIGINT, outlier BOOLEAN)",
    "CREATE TABLE event_json (event_id TEXT, json TEXT)",
)


class TestBackfill(StartupCase):
    def setUp(self) -> None:
        super().setUp()
        connection = self.hs.store.db_pool.connection
        for sql in _SYNAPSE_TABLES:
            connection.execute(sql)
        connection.execute(CREATE_TABLE_SQL)
        connection.commit()

    def _event(
        self,
        event_id: str,
        room_id: str,
        position: int,
        sender: str = STUDENT,
        content: Optional[Dict[str, Any]] = None,
    ) -> None:
        connection = self.hs.store.db_pool.connection
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, 'm.room.message', ?, 0)",
            (event_id, room_id, sender, position),
        )
        if content is not None:
            connection.execute(
                "INSERT INTO event_json VALUES (?, ?)",
                (event_id, json.dumps({"content": content, "sender": sender})),
            )
        connection.commit()

    def _decision(self, event_id: str, disposition: str, category: str) -> None:
        self.hs.store.db_pool.connection.execute(
            "INSERT INTO pangea_moderation_disposition VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, ROOM, disposition, category, 500, "claim"),
        )
        self.hs.store.db_pool.connection.commit()

    async def test_the_legacy_decisions_are_copied_with_the_courses_of_their_time(
        self,
    ) -> None:
        """The learner was in course A when the message was sent and has left
        since; they joined course B after it. The row belongs to A only."""
        self.world.rename(ROOM, "Then")
        sent_at = self.world.position
        self.world.rename(ROOM, "Now")
        self.world.leave(STUDENT, COURSE_A)
        self.world.join(STUDENT, COURSE_B)
        self._event(
            "$msg", ROOM, sent_at, content={"msgtype": "m.text", "body": "awful"}
        )
        self._decision("$msg", "redacted", "harassment")
        self.hs.store.events["$msg"] = FakeEvent(event_id="$msg", redacted=True)

        self.assertEqual(await self._startup().backfill(), 1)
        row = await self.store.get("mod:$msg")
        assert row is not None
        self.assertEqual(row.course_ids, (COURSE_A,))
        self.assertEqual(row.room_name, "Then", "the backfill read today's name")
        self.assertEqual(row.action, ACTION_REDACTED)
        self.assertEqual(row.outcome, OUTCOME_REMOVED)
        self.assertEqual(row.text, "awful", "the unpruned JSON was not read")
        self.assertEqual(row.subject_id, STUDENT)
        self.assertEqual(row.created_ms, 500)

    async def test_every_legacy_preserve_is_self_harm(self) -> None:
        self._event("$p", ROOM, 5, content={"msgtype": "m.text", "body": "help"})
        self._decision("$p", "preserved", "harassment")
        self.hs.store.events["$p"] = FakeEvent(event_id="$p")
        await self._startup().backfill()
        row = await self.store.get("mod:$p")
        assert row is not None
        self.assertTrue(row.self_harm, "a legacy preserve was not marked self-harm")
        self.assertEqual((row.action, row.outcome), (ACTION_PRESERVED, None))

    async def test_pruned_text_is_null_and_a_standing_redaction_is_unknown(
        self,
    ) -> None:
        self._event("$pruned", ROOM, 5, content={})
        self._decision("$pruned", "redacted", "hate")
        self.hs.store.events["$pruned"] = FakeEvent(event_id="$pruned")
        await self._startup().backfill()
        row = await self.store.get("mod:$pruned")
        assert row is not None
        self.assertIsNone(row.text)
        self.assertEqual(row.outcome, OUTCOME_UNKNOWN)
        self.assertFalse(row.self_harm)

    async def test_it_runs_once_and_never_overwrites_a_live_row(self) -> None:
        self._event("$m", ROOM, 5, content={"msgtype": "m.text", "body": "x"})
        self._decision("$m", "redacted", "hate")
        await self.store.insert(
            _incident(
                incident_id="mod:$m",
                event_id="$m",
                text="live",
                action=ACTION_PRESERVED,
            )
        )
        startup = self._startup()
        self.assertEqual(await startup.backfill(), 0)
        row = await self.store.get("mod:$m")
        assert row is not None
        self.assertEqual((row.text, row.action), ("live", ACTION_PRESERVED))
        self._event("$later", ROOM, 6, content={"msgtype": "m.text", "body": "y"})
        self._decision("$later", "redacted", "hate")
        self.assertEqual(await startup.backfill(), 0, "the backfill ran twice")
        self.assertIsNone(await self.store.get("mod:$later"))

    async def test_a_row_that_fails_leaves_the_backfill_to_run_again(self) -> None:
        self._event("$ok", ROOM, 5, content={"msgtype": "m.text", "body": "x"})
        self._decision("$ok", "redacted", "hate")
        self._decision("$bad", "redacted", "hate")
        self._event("$bad", ROOM, 6, content={"msgtype": "m.text", "body": "y"})
        self.world.error = RuntimeError("no state group")
        await self._startup().backfill()
        self.world.error = None
        self.assertFalse(await self.store.is_done("disposition_backfill"))
        healed = self._startup()
        await healed.backfill()
        self.assertTrue(await self.store.is_done("disposition_backfill"))
        self.assertIsNotNone(await self.store.get("mod:$bad"))

    async def test_an_instance_that_never_ran_tier2_backfills_nothing(self) -> None:
        self.hs.store.db_pool.connection.execute(
            "DROP TABLE pangea_moderation_disposition"
        )
        self.assertEqual(await self._startup().backfill(), 0)
        self.assertTrue(await self.store.is_done("disposition_backfill"))

    def test_every_backfill_statement_is_a_read(self) -> None:
        for sql in STARTUP_STATEMENTS:
            self.assertTrue(sql.strip().upper().startswith("SELECT"), sql)
