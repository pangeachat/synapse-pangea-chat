"""The Safety page's record: `pangea_safety_incidents` (admin-dash#105).

Two principles are under test, and most tests here are one of them:

- **Snapshot at the moment of the incident.** Sender, text and course are
  captured when it happens and never recomputed - so a learner who leaves a
  course before the verdict still lands on it.
- **Never enforce without a record.** No redaction is sent unless the
  incident row was written first.

The store runs against a real SQLite database through Synapse's own
parameter converter (`tests/moderation_doubles.py` says why), and Tier 1 and
Tier 2 are driven through the real `ChatModeration` handlers.
"""

import logging
import unittest
from types import SimpleNamespace
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    cast,
)
from unittest.mock import AsyncMock, create_autospec, patch

from synapse.module_api import ModuleApi

from synapse_pangea_chat.moderation import ChatModeration
from synapse_pangea_chat.moderation.choreo_client import moderate_text
from synapse_pangea_chat.moderation.courses import (
    COURSE_PLAN_EVENT_TYPE,
    CREATE_EVENT_TYPE,
    MEMBER_EVENT_TYPE,
    POWER_LEVELS_EVENT_TYPE,
    StudentCourses,
    is_course_admin_in,
    is_student_in,
)
from synapse_pangea_chat.moderation.dispatch import ModerationJob
from synapse_pangea_chat.moderation.incidents import (
    ACTION_BLOCKED,
    ACTION_KEPT,
    ACTION_PRESERVED,
    ACTION_REDACTED,
    ACTION_REPORTED,
    OUTCOME_FAILED,
    OUTCOME_PENDING,
    OUTCOME_REMOVED,
    OUTCOME_SKIPPED,
    OUTCOME_UNKNOWN,
    SOURCE_MODERATION,
    STATEMENTS,
    Incident,
    IncidentStore,
    merge_action,
    merge_incident,
    merge_outcome,
    next_updated_ms,
)
from synapse_pangea_chat.moderation.recorder import IncidentRecorder

from .moderation_doubles import (
    DbPoolDouble,
    EventStoreDouble,
    HomeServerDouble,
    MetricReader,
    sqlite_engine,
    tier1_refusal_code,
)
from .test_moderation_unit import (
    MODERATE_TEXT,
    _config,
    _event,
    _module_api,
    _tier2_config,
    _tier2_module,
)

STUDENT = "@learner:example.org"
TEACHER = "@teacher:example.org"
OUTSIDER = "@outsider:example.org"
COURSE_A = "!course-a:example.org"
COURSE_B = "!course-b:example.org"
ROOM = "!room:example.org"


# ---------------------------------------------------------------------------
# A small world of courses, for the course rule
# ---------------------------------------------------------------------------


def _state_event(content: Mapping[str, Any], sender: str = TEACHER) -> Any:
    return SimpleNamespace(
        content=dict(content),
        sender=sender,
        room_version=SimpleNamespace(msc4289_creator_power_enabled=False),
    )


class CourseWorld:
    """Rooms, their course plans and power levels, and who is joined where.

    Mutable on purpose: "the learner leaves before the verdict" is a test
    that changes the world between the queue and the verdict.
    """

    def __init__(self) -> None:
        self.plans: Dict[str, bool] = {}
        self.levels: Dict[str, Dict[str, int]] = {}
        self.joined: Dict[str, Set[str]] = {}
        self.reads: List[Tuple[str, str]] = []
        self.error: Optional[Exception] = None

    def room(self, room_id: str, *, course: bool, **levels: int) -> None:
        self.plans[room_id] = course
        self.levels[room_id] = {f"@{k}:example.org": v for k, v in levels.items()}

    def join(self, user_id: str, room_id: str) -> None:
        self.joined.setdefault(user_id, set()).add(room_id)

    def leave(self, user_id: str, room_id: str) -> None:
        self.joined.get(user_id, set()).discard(room_id)

    async def rooms_for_user(self, user_id: str) -> frozenset:
        if self.error is not None:
            raise self.error
        return frozenset(self.joined.get(user_id, set()))

    async def read_state(self, room_id: str, user_id: str) -> Dict[Any, Any]:
        self.reads.append((room_id, user_id))
        state: Dict[Any, Any] = {
            (CREATE_EVENT_TYPE, ""): _state_event({"type": "m.space"}),
            (POWER_LEVELS_EVENT_TYPE, ""): _state_event(
                {"users": dict(self.levels.get(room_id, {}))}
            ),
        }
        if self.plans.get(room_id):
            state[(COURSE_PLAN_EVENT_TYPE, "")] = _state_event({"uuid": "plan"})
        if room_id in self.joined.get(user_id, set()):
            state[(MEMBER_EVENT_TYPE, user_id)] = _state_event(
                {"membership": "join"}, sender=user_id
            )
        return state

    def courses(
        self, is_exempt: Callable[[str], bool] = lambda _user: False
    ) -> StudentCourses:
        return StudentCourses(
            rooms_for_user=self.rooms_for_user,
            read_state=self.read_state,
            is_exempt=is_exempt,
        )


def _classroom() -> CourseWorld:
    """A learner and a teacher in course A; the teacher also teaches B and
    is a learner in nothing; the learner also sits in an ordinary room."""
    world = CourseWorld()
    world.room(COURSE_A, course=True, teacher=100)
    world.room(COURSE_B, course=True, teacher=100)
    world.room(ROOM, course=False)
    for room in (COURSE_A, ROOM):
        world.join(STUDENT, room)
    for room in (COURSE_A, COURSE_B, ROOM):
        world.join(TEACHER, room)
    return world


def _incident(**overrides: Any) -> Incident:
    fields: Dict[str, Any] = {
        "incident_id": "mod:$e1",
        "source": SOURCE_MODERATION,
        "action": ACTION_KEPT,
        "outcome": None,
        "subject_id": STUDENT,
        "reporter_id": None,
        "room_id": ROOM,
        "event_id": "$e1",
        "course_ids": (COURSE_A,),
        "categories": ("harassment",),
        "self_harm": False,
        "rule": None,
        "top_score": 0.3,
        "text": "the text",
        "reason": None,
        "created_ms": 1000,
        "updated_ms": 1000,
    }
    fields.update(overrides)
    return Incident(**fields)


def _store(db_pool: Optional[DbPoolDouble] = None) -> Tuple[IncidentStore, Any]:
    store = EventStoreDouble()
    if db_pool is not None:
        store.db_pool = db_pool
    homeserver = HomeServerDouble(store)
    return IncidentStore(homeserver), homeserver


def _rows(db_pool: DbPoolDouble) -> List[Tuple[Any, ...]]:
    exists = db_pool.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'pangea_safety_incidents'"
    ).fetchone()
    if exists is None:
        return []
    return db_pool.connection.execute(
        "SELECT incident_id, action, outcome, course_ids, categories, self_harm, "
        "text, reason, subject_id, event_id, rule, top_score, updated_ms "
        "FROM pangea_safety_incidents ORDER BY incident_id"
    ).fetchall()


# ---------------------------------------------------------------------------
# The merge rules
# ---------------------------------------------------------------------------


class TestMergeRules(unittest.TestCase):
    def test_categories_are_unioned_in_first_seen_order(self) -> None:
        merged = merge_incident(
            _incident(categories=("harassment", "hate")),
            _incident(categories=("hate", "violence")),
        )
        self.assertEqual(merged.categories, ("harassment", "hate", "violence"))

    def test_self_harm_only_turns_on(self) -> None:
        on = merge_incident(_incident(self_harm=False), _incident(self_harm=True))
        self.assertTrue(on.self_harm)
        stays = merge_incident(_incident(self_harm=True), _incident(self_harm=False))
        self.assertTrue(stays.self_harm, "a later verdict cleared self_harm")

    def test_the_score_keeps_its_maximum(self) -> None:
        self.assertEqual(
            merge_incident(
                _incident(top_score=0.9), _incident(top_score=0.2)
            ).top_score,
            0.9,
        )
        self.assertEqual(
            merge_incident(
                _incident(top_score=0.2), _incident(top_score=0.9)
            ).top_score,
            0.9,
        )
        self.assertEqual(
            merge_incident(
                _incident(top_score=0.4), _incident(top_score=None)
            ).top_score,
            0.4,
        )
        self.assertEqual(
            merge_incident(
                _incident(top_score=None), _incident(top_score=0.4)
            ).top_score,
            0.4,
        )

    def test_the_action_only_moves_up(self) -> None:
        order = [ACTION_KEPT, ACTION_REDACTED, ACTION_PRESERVED]
        for i, lower in enumerate(order):
            for higher in order[i:]:
                self.assertEqual(merge_action(lower, higher), higher)
                self.assertEqual(
                    merge_action(higher, lower),
                    higher,
                    f"{lower} after {higher} moved the action down",
                )

    def test_an_action_outside_the_order_is_kept(self) -> None:
        self.assertEqual(merge_action(ACTION_BLOCKED, ACTION_PRESERVED), ACTION_BLOCKED)
        self.assertEqual(merge_action(ACTION_REPORTED, ACTION_KEPT), ACTION_REPORTED)

    def test_no_attempt_never_changes_the_outcome(self) -> None:
        for existing in (
            None,
            OUTCOME_PENDING,
            OUTCOME_REMOVED,
            OUTCOME_FAILED,
            OUTCOME_SKIPPED,
            OUTCOME_UNKNOWN,
        ):
            self.assertEqual(merge_outcome(existing, None), existing)

    def test_removed_is_final(self) -> None:
        for new in (OUTCOME_PENDING, OUTCOME_FAILED, OUTCOME_SKIPPED, OUTCOME_UNKNOWN):
            self.assertEqual(merge_outcome(OUTCOME_REMOVED, new), OUTCOME_REMOVED)

    def test_a_later_attempt_supersedes_failed_skipped_and_unknown(self) -> None:
        for existing in (OUTCOME_FAILED, OUTCOME_SKIPPED, OUTCOME_UNKNOWN, None):
            for new in (
                OUTCOME_PENDING,
                OUTCOME_REMOVED,
                OUTCOME_FAILED,
                OUTCOME_UNKNOWN,
            ):
                self.assertEqual(merge_outcome(existing, new), new)

    def test_skipped_fills_only_an_empty_outcome(self) -> None:
        self.assertEqual(merge_outcome(None, OUTCOME_SKIPPED), OUTCOME_SKIPPED)
        for existing in (OUTCOME_FAILED, OUTCOME_UNKNOWN, OUTCOME_PENDING):
            self.assertEqual(
                merge_outcome(existing, OUTCOME_SKIPPED),
                existing,
                "a skipped redaction erased what an earlier attempt established",
            )

    def test_the_outcome_is_independent_of_the_action(self) -> None:
        merged = merge_incident(
            _incident(action=ACTION_REDACTED, outcome=OUTCOME_REMOVED),
            _incident(action=ACTION_PRESERVED, outcome=None),
        )
        self.assertEqual(
            (merged.action, merged.outcome), (ACTION_PRESERVED, OUTCOME_REMOVED)
        )

    def test_the_snapshot_is_the_first_writes(self) -> None:
        merged = merge_incident(
            _incident(text="first", course_ids=(COURSE_A,), created_ms=1),
            _incident(text="second", course_ids=(COURSE_B,), created_ms=2),
        )
        self.assertEqual(
            (merged.text, merged.course_ids, merged.created_ms),
            ("first", (COURSE_A,), 1),
        )

    def test_updated_ms_strictly_increases(self) -> None:
        self.assertEqual(next_updated_ms(5000, 4000), 5001)
        self.assertEqual(next_updated_ms(5000, 5000), 5001)
        self.assertEqual(next_updated_ms(5000, 9000), 9000)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class TestIncidentSqlRunsOnSqlite(unittest.TestCase):
    def test_every_statement_names_a_safety_table(self) -> None:
        for sql in STATEMENTS:
            self.assertIn("pangea_safety_incident", sql)

    def test_the_statements_parse_on_sqlite(self) -> None:
        import sqlite3

        connection = sqlite3.connect(":memory:")
        try:
            for sql in STATEMENTS[:4]:
                connection.execute(sqlite_engine().convert_param_style(sql))
        finally:
            connection.close()


class TestIncidentStore(unittest.IsolatedAsyncioTestCase):
    async def test_a_course_reads_its_incidents_in_order_and_no_others(self) -> None:
        db_pool = DbPoolDouble()
        store, _hs = _store(db_pool)
        await store.insert(_incident(incident_id="mod:$b", created_ms=20))
        await store.insert(_incident(incident_id="mod:$a", created_ms=20))
        await store.insert(_incident(incident_id="mod:$c", created_ms=10))
        await store.insert(
            _incident(incident_id="mod:$other", course_ids=(COURSE_B,), created_ms=5)
        )
        await store.insert(
            _incident(
                incident_id="mod:$both", course_ids=(COURSE_A, COURSE_B), created_ms=30
            )
        )
        rows = await store.for_course(COURSE_A)
        self.assertEqual(
            [row.incident_id for row in rows],
            ["mod:$c", "mod:$a", "mod:$b", "mod:$both"],
        )
        self.assertEqual(
            [row.incident_id for row in await store.for_course(COURSE_B)],
            ["mod:$other", "mod:$both"],
        )

    async def test_insert_is_idempotent_and_returns_the_first_write(self) -> None:
        db_pool = DbPoolDouble()
        store, _hs = _store(db_pool)
        first = await store.insert(_incident(incident_id="report:r1", text="first"))
        again = await store.insert(_incident(incident_id="report:r1", text="second"))
        self.assertEqual(again.text, "first")
        self.assertEqual(first.text, "first")
        self.assertEqual(len(_rows(db_pool)), 1)

    async def test_a_row_is_never_written_without_its_courses(self) -> None:
        store, _hs = _store()
        with self.assertRaises(ValueError):
            await store.insert(_incident(course_ids=None))
        with self.assertRaises(ValueError):
            await store.upsert_verdict(_incident(course_ids=None))

    async def test_nul_is_replaced_and_everything_else_is_verbatim(self) -> None:
        db_pool = DbPoolDouble()
        store, _hs = _store(db_pool)
        await store.insert(
            _incident(
                text="a\x00b é\n\t<b>", reason="why\x00not", course_ids=(COURSE_A,)
            )
        )
        row = (await store.for_course(COURSE_A))[0]
        self.assertEqual(row.text, "a␀b é\n\t<b>")
        self.assertEqual(row.reason, "why␀not")

    async def test_the_merge_lands_and_updated_ms_moves_only_on_a_change(self) -> None:
        db_pool = DbPoolDouble()
        store, homeserver = _store(db_pool)
        await store.upsert_verdict(_incident(updated_ms=1000))
        before = await store.get("mod:$e1")
        assert before is not None
        await store.upsert_verdict(_incident(updated_ms=1000))
        unchanged = await store.get("mod:$e1")
        self.assertEqual(unchanged, before, "a repeat with nothing new changed the row")

        await store.upsert_verdict(
            _incident(
                action=ACTION_PRESERVED,
                categories=("self_harm",),
                self_harm=True,
                top_score=0.95,
            )
        )
        after = await store.get("mod:$e1")
        assert after is not None
        self.assertEqual(after.action, ACTION_PRESERVED)
        self.assertEqual(after.categories, ("harassment", "self_harm"))
        self.assertTrue(after.self_harm)
        self.assertEqual(after.top_score, 0.95)
        self.assertGreater(after.updated_ms, before.updated_ms)

    async def test_upsert_returns_the_outcome_it_replaced(self) -> None:
        store, _hs = _store()
        self.assertIsNone(await store.upsert_verdict(_incident(outcome=OUTCOME_FAILED)))
        self.assertEqual(
            await store.upsert_verdict(_incident(outcome=OUTCOME_PENDING)),
            OUTCOME_FAILED,
        )

    async def test_set_outcome_never_replaces_removed(self) -> None:
        store, _hs = _store()
        await store.insert(_incident(outcome=OUTCOME_REMOVED))
        self.assertFalse(await store.set_outcome("mod:$e1", OUTCOME_FAILED))
        row = await store.get("mod:$e1")
        assert row is not None
        self.assertEqual(row.outcome, OUTCOME_REMOVED)

    async def test_set_outcome_can_be_conditional(self) -> None:
        store, homeserver = _store()
        await store.insert(_incident(outcome=OUTCOME_PENDING, updated_ms=5000))
        self.assertFalse(
            await store.set_outcome(
                "mod:$e1",
                OUTCOME_UNKNOWN,
                only_if=OUTCOME_PENDING,
                updated_before_ms=5000,
            ),
            "a row updated at or after the cutoff was settled",
        )
        self.assertTrue(
            await store.set_outcome(
                "mod:$e1",
                OUTCOME_UNKNOWN,
                only_if=OUTCOME_PENDING,
                updated_before_ms=5001,
            )
        )
        self.assertFalse(
            await store.set_outcome("mod:$e1", OUTCOME_REMOVED, only_if=OUTCOME_PENDING)
        )


# ---------------------------------------------------------------------------
# The course rule
# ---------------------------------------------------------------------------


class TestStudentCourses(unittest.IsolatedAsyncioTestCase):
    async def test_a_student_course_is_a_joined_course_below_100(self) -> None:
        world = _classroom()
        courses = world.courses()
        self.assertEqual(await courses.for_user(STUDENT), (COURSE_A,))
        self.assertEqual(
            await courses.for_user(TEACHER), (), "a course admin is nobody's student"
        )

    async def test_a_room_without_a_course_plan_is_not_a_course(self) -> None:
        world = _classroom()
        world.room(ROOM, course=False)
        self.assertNotIn(ROOM, await world.courses().for_user(STUDENT))

    async def test_exempt_users_and_bots_are_nobodys_students(self) -> None:
        world = _classroom()
        bot = "@bot:example.org"
        world.join(bot, COURSE_A)
        exempt = world.courses(is_exempt=lambda user: user == STUDENT)
        self.assertEqual(await exempt.for_user(STUDENT), ())
        self.assertEqual(await world.courses().for_user(bot), ())

    async def test_a_read_that_fails_is_an_error_and_not_a_student_nowhere(
        self,
    ) -> None:
        world = _classroom()
        world.error = RuntimeError("store down")
        with self.assertRaises(RuntimeError):
            await world.courses().for_user(STUDENT)

    async def test_a_teachers_report_reaches_only_the_learners_course(self) -> None:
        """The teacher teaches A and B; the learner is in A. A report the
        teacher files about the learner must reach A only - B's admins have
        no business seeing a message from a learner who is not theirs."""
        world = _classroom()
        self.assertEqual(
            await world.courses().for_report(STUDENT, TEACHER), (COURSE_A,)
        )

    async def test_a_reporters_own_student_courses_are_not_added(self) -> None:
        """The reporter is a LEARNER in another course too. Their own
        courses are a fallback for a subject who is a student nowhere, never
        an addition: course C's admins must not see a message from course A."""
        world = _classroom()
        course_c = "!course-c:example.org"
        world.room(course_c, course=True, someone=100)
        world.join(TEACHER, course_c)
        self.assertEqual(await world.courses().for_user(TEACHER), (course_c,))
        self.assertEqual(
            await world.courses().for_report(STUDENT, TEACHER), (COURSE_A,)
        )

    async def test_a_report_about_an_outsider_goes_to_the_reporters_courses(
        self,
    ) -> None:
        world = _classroom()
        world.join(OUTSIDER, ROOM)
        self.assertEqual(
            await world.courses().for_report(OUTSIDER, STUDENT), (COURSE_A,)
        )

    async def test_nobodys_report_reaches_no_course(self) -> None:
        world = _classroom()
        self.assertEqual(await world.courses().for_report(OUTSIDER, TEACHER), ())

    async def test_admin_and_student_predicates(self) -> None:
        world = _classroom()
        state = await world.read_state(COURSE_A, TEACHER)
        self.assertTrue(is_course_admin_in(state, TEACHER))
        self.assertFalse(is_student_in(state, TEACHER))
        state = await world.read_state(COURSE_A, STUDENT)
        self.assertTrue(is_student_in(state, STUDENT))
        self.assertFalse(is_course_admin_in(state, STUDENT))
        world.leave(TEACHER, COURSE_A)
        departed = await world.read_state(COURSE_A, TEACHER)
        self.assertFalse(
            is_course_admin_in(departed, TEACHER), "a departed admin still counts"
        )
        plain = await world.read_state(ROOM, TEACHER)
        self.assertFalse(is_course_admin_in(plain, TEACHER))


# ---------------------------------------------------------------------------
# Tier 1
# ---------------------------------------------------------------------------

PHONE_TEXT = "call me at 415-555-2671"


def _tier1(world: CourseWorld) -> Tuple[ChatModeration, HomeServerDouble, DbPoolDouble]:
    homeserver = HomeServerDouble()
    module = ChatModeration(_module_api(homeserver), _config())
    module._recorder.courses = world.courses()
    return module, homeserver, homeserver.store.db_pool


class TestTier1RecordsEveryBlock(unittest.IsolatedAsyncioTestCase):
    async def test_a_block_is_written_before_the_refusal(self) -> None:
        world = _classroom()
        module, _hs, db_pool = _tier1(world)
        result = await module.check_event_for_spam(_event(PHONE_TEXT, sender=STUDENT))
        tier1_refusal_code(result)
        rows = _rows(db_pool)
        self.assertEqual(len(rows), 1)
        (
            incident_id,
            action,
            outcome,
            course_ids,
            _cats,
            self_harm,
            text,
            _r,
            subject,
            event_id,
            rule,
            _score,
            _updated,
        ) = rows[0]
        self.assertTrue(incident_id.startswith("block:"))
        self.assertEqual(action, ACTION_BLOCKED)
        self.assertIsNone(outcome)
        self.assertEqual(course_ids, f'["{COURSE_A}"]')
        self.assertEqual(text, PHONE_TEXT)
        self.assertEqual(subject, STUDENT)
        self.assertIsNone(event_id)
        self.assertEqual(rule, "contact_details")
        self.assertFalse(self_harm)

    async def test_a_clean_message_writes_nothing(self) -> None:
        module, _hs, db_pool = _tier1(_classroom())
        await module.check_event_for_spam(_event("hello there", sender=STUDENT))
        self.assertEqual(db_pool.interactions, [])

    async def test_a_failed_write_still_refuses_and_is_retried(self) -> None:
        world = _classroom()
        module, homeserver, db_pool = _tier1(world)
        reader = MetricReader()
        reader.snapshot("pangea_safety_incident_write_failed_total", kind="block")
        db_pool.error = RuntimeError("database down")
        result = await module.check_event_for_spam(_event(PHONE_TEXT, sender=STUDENT))
        tier1_refusal_code(result)
        self.assertEqual(
            reader.delta("pangea_safety_incident_write_failed_total", kind="block"), 1.0
        )
        db_pool.error = None
        homeserver.clock.advance(1.0)
        rows = _rows(db_pool)
        self.assertEqual(len(rows), 1, "the captured block was not retried")
        self.assertEqual(rows[0][6], PHONE_TEXT)

    async def test_a_course_lookup_that_fails_is_retried_with_the_row(self) -> None:
        world = _classroom()
        module, homeserver, db_pool = _tier1(world)
        world.error = RuntimeError("state unreadable")
        tier1_refusal_code(
            await module.check_event_for_spam(_event(PHONE_TEXT, sender=STUDENT))
        )
        self.assertEqual(_rows(db_pool), [], "a row was written with no courses")
        world.error = None
        homeserver.clock.advance(1.0)
        self.assertEqual(_rows(db_pool)[0][3], f'["{COURSE_A}"]')

    async def test_five_failed_retries_lose_the_row_loudly(self) -> None:
        module, homeserver, db_pool = _tier1(_classroom())
        reader = MetricReader()
        reader.snapshot("pangea_safety_incident_lost_total", kind="block")
        db_pool.error = RuntimeError("database down")
        with self.assertLogs(
            "synapse.modules.synapse_pangea_chat.moderation", level="ERROR"
        ) as logs:
            tier1_refusal_code(
                await module.check_event_for_spam(_event(PHONE_TEXT, sender=STUDENT))
            )
            attempts_before = len(db_pool.interactions)
            for seconds in (1, 2, 4, 8, 16):
                homeserver.clock.advance(seconds)
        self.assertEqual(
            len(db_pool.interactions) - attempts_before, 5, "not five retries"
        )
        self.assertEqual(
            reader.delta("pangea_safety_incident_lost_total", kind="block"), 1.0
        )
        self.assertTrue(any("not written after every retry" in m for m in logs.output))
        homeserver.clock.advance(64)
        self.assertEqual(len(db_pool.interactions) - attempts_before, 5)


# ---------------------------------------------------------------------------
# Tier 2
# ---------------------------------------------------------------------------


def _verdict(*categories: str, scores: Optional[Dict[str, float]] = None) -> AsyncMock:
    mock = create_autospec(moderate_text)
    result: Dict[str, Any] = {
        "flagged": True,
        "categories": list(categories),
        "evaluated": True,
    }
    if scores is not None:
        result["category_scores"] = scores
    mock.return_value = result
    return cast(AsyncMock, mock)


def _job(
    event_id: str = "$e1", text: str = "you are awful", **extra: Any
) -> ModerationJob:
    return ModerationJob(
        event_id=event_id,
        room_id=ROOM,
        sender=STUDENT,
        text=text,
        enqueued_at=0.0,
        **extra,
    )


class Tier2Case(unittest.IsolatedAsyncioTestCase):
    def _module(
        self,
        world: Optional[CourseWorld] = None,
        store: Optional[EventStoreDouble] = None,
    ) -> Tuple[ChatModeration, ModuleApi, HomeServerDouble, DbPoolDouble]:
        homeserver = HomeServerDouble(store)
        api = _module_api(homeserver)
        module = _tier2_module(self, api, _tier2_config())
        module._recorder.courses = (world or _classroom()).courses()
        return module, api, homeserver, homeserver.store.db_pool

    async def _row(self, module: ChatModeration, event_id: str = "$e1") -> Incident:
        row = await module._incidents.get(f"mod:{event_id}")
        assert row is not None, "no incident row"
        return row


class TestTier2RecordsEveryVerdict(Tier2Case):
    async def test_below_threshold_is_recorded_kept(self) -> None:
        module, api, _hs, _db = self._module()
        with patch(MODERATE_TEXT, _verdict("harassment", scores={"harassment": 0.3})):
            await module._check_and_redact(_job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        row = await self._row(module)
        self.assertEqual(
            (row.action, row.outcome, row.source),
            (ACTION_KEPT, None, SOURCE_MODERATION),
        )
        self.assertEqual(row.categories, ("harassment",))
        self.assertEqual(row.top_score, 0.3)
        self.assertEqual(row.course_ids, (COURSE_A,))
        self.assertEqual(row.text, "you are awful")
        self.assertEqual(row.subject_id, STUDENT)
        self.assertEqual(row.event_id, "$e1")

    async def test_self_harm_is_recorded_preserved_and_not_redacted(self) -> None:
        module, api, _hs, _db = self._module()
        with patch(MODERATE_TEXT, _verdict("harassment", "self-harm/intent")):
            await module._check_and_redact(_job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        row = await self._row(module)
        self.assertEqual((row.action, row.outcome), (ACTION_PRESERVED, None))
        self.assertTrue(row.self_harm)
        self.assertEqual(row.categories, ("harassment", "self_harm"))

    async def test_an_unrecognised_self_harm_name_is_still_self_harm(self) -> None:
        module, _api, _hs, _db = self._module()
        with patch(MODERATE_TEXT, _verdict("self-harm/invented")):
            await module._check_and_redact(_job())
        row = await self._row(module)
        self.assertTrue(row.self_harm)
        self.assertIn("self_harm", row.categories)

    async def test_a_redaction_is_sent_only_after_its_row_exists(self) -> None:
        module, api, _hs, _db = self._module()
        seen: List[Optional[Incident]] = []

        async def _send(event_dict: Dict[str, Any]) -> None:
            seen.append(await module._incidents.get("mod:$e1"))

        cast(AsyncMock, api.create_and_send_event_into_room).side_effect = _send
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        self.assertEqual(len(seen), 1)
        at_send = seen[0]
        assert at_send is not None, "the redaction was sent before its row existed"
        self.assertEqual(
            (at_send.action, at_send.outcome), (ACTION_REDACTED, OUTCOME_PENDING)
        )
        row = await self._row(module)
        self.assertEqual(row.outcome, OUTCOME_REMOVED)

    async def test_no_redaction_without_a_written_row(self) -> None:
        """The incident table refuses; nothing else does. The message must
        stay up, the skip must be counted, and the captured row - with no
        outcome, because nothing was attempted - must land on the retry."""
        module, api, homeserver, db_pool = self._module()
        refuse = {"on": True}

        def _refuse_incidents(sql: str, _args: Any) -> None:
            if refuse["on"] and "pangea_safety_incidents" in sql:
                raise RuntimeError("incident table refuses")

        db_pool.on_statement = _refuse_incidents
        reader = MetricReader()
        reader.snapshot(
            "pangea_moderation_tier2_redaction_skipped_total",
            cause="incident_unwritten",
        )
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        self.assertEqual(
            reader.delta(
                "pangea_moderation_tier2_redaction_skipped_total",
                cause="incident_unwritten",
            ),
            1.0,
        )
        refuse["on"] = False
        homeserver.clock.advance(1.0)
        row = await self._row(module)
        self.assertEqual((row.action, row.outcome), (ACTION_REDACTED, None))

    async def test_a_course_lookup_failure_is_no_redaction_either(self) -> None:
        world = _classroom()
        module, api, _hs, _db = self._module(world)
        world.error = RuntimeError("state unreadable")
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_a_repeat_verdict_merges(self) -> None:
        module, api, _hs, _db = self._module()
        with patch(MODERATE_TEXT, _verdict("harassment", scores={"harassment": 0.3})):
            await module._check_and_redact(_job())
        with patch(MODERATE_TEXT, _verdict("self-harm/intent")):
            await module._check_and_redact(_job())
        with patch(MODERATE_TEXT, _verdict("hate", scores={"hate": 0.1})):
            await module._check_and_redact(_job())
        row = await self._row(module)
        self.assertEqual(row.action, ACTION_PRESERVED)
        self.assertEqual(row.categories, ("harassment", "self_harm", "hate"))
        self.assertTrue(row.self_harm)
        self.assertEqual(row.top_score, 0.3)
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_a_send_that_raises_and_did_not_land_is_failed(self) -> None:
        module, api, _hs, _db = self._module()
        cast(AsyncMock, api.create_and_send_event_into_room).side_effect = RuntimeError(
            "send refused"
        )
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        self.assertEqual((await self._row(module)).outcome, OUTCOME_FAILED)

    async def test_a_send_that_raises_after_landing_is_removed(self) -> None:
        store = EventStoreDouble()
        module, api, _hs, _db = self._module(store=store)

        async def _land_then_raise(event_dict: Dict[str, Any]) -> None:
            store.redacted = True
            raise RuntimeError("raised after the write")

        cast(
            AsyncMock, api.create_and_send_event_into_room
        ).side_effect = _land_then_raise
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        self.assertEqual((await self._row(module)).outcome, OUTCOME_REMOVED)

    async def test_a_send_whose_landing_cannot_be_read_is_unknown(self) -> None:
        store = EventStoreDouble()
        module, api, _hs, _db = self._module(store=store)

        async def _vanish_then_raise(event_dict: Dict[str, Any]) -> None:
            store.missing = True
            raise RuntimeError("raised")

        cast(
            AsyncMock, api.create_and_send_event_into_room
        ).side_effect = _vanish_then_raise
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        self.assertEqual((await self._row(module)).outcome, OUTCOME_UNKNOWN)

    async def test_a_message_already_gone_is_removed(self) -> None:
        module, api, _hs, _db = self._module(store=EventStoreDouble(redacted=True))
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        self.assertEqual((await self._row(module)).outcome, OUTCOME_REMOVED)

    async def test_a_redaction_declined_by_the_claim_is_skipped(self) -> None:
        module, api, _hs, _db = self._module()
        assert module._dispatcher is not None
        with patch.object(
            type(module._dispatcher),
            "actions_permitted",
            new_callable=lambda: property(lambda _self: False),
        ):
            with patch(MODERATE_TEXT, _verdict("harassment")):
                await module._check_and_redact(_job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        self.assertEqual((await self._row(module)).outcome, OUTCOME_SKIPPED)

    async def test_a_skip_puts_back_what_an_earlier_attempt_established(self) -> None:
        module, api, _hs, _db = self._module()
        cast(AsyncMock, api.create_and_send_event_into_room).side_effect = RuntimeError(
            "send refused"
        )
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        self.assertEqual((await self._row(module)).outcome, OUTCOME_FAILED)
        assert module._dispatcher is not None
        with patch.object(
            type(module._dispatcher),
            "actions_permitted",
            new_callable=lambda: property(lambda _self: False),
        ):
            with patch(MODERATE_TEXT, _verdict("harassment")):
                await module._check_and_redact(_job())
        self.assertEqual((await self._row(module)).outcome, OUTCOME_FAILED)


class TestCourseIsFrozenAtQueueTime(Tier2Case):
    async def test_a_learner_who_leaves_before_the_verdict_lands_on_the_course(
        self,
    ) -> None:
        world = _classroom()
        module, api, homeserver, _db = self._module(world)
        assert module._dispatcher is not None
        queued: List[ModerationJob] = []
        with patch.object(
            module._dispatcher,
            "enqueue",
            side_effect=lambda job: queued.append(job) or True,
        ):
            await module.on_new_event(
                _event("you are awful", sender=STUDENT, event_id="$e1"), {}
            )
        self.assertEqual(len(queued), 1)
        self.assertIsNotNone(queued[0].courses, "no course snapshot rode on the job")
        homeserver.clock.run_pending()
        world.leave(STUDENT, COURSE_A)
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(queued[0])
        row = await self._row(module)
        self.assertEqual(
            row.course_ids,
            (COURSE_A,),
            "the course was read at verdict time, after the learner had left",
        )

    async def test_on_new_event_does_no_course_reads_inline(self) -> None:
        world = _classroom()
        module, _api, homeserver, _db = self._module(world)
        assert module._dispatcher is not None
        with patch.object(module._dispatcher, "enqueue", side_effect=lambda job: True):
            await module.on_new_event(_event("hello", sender=STUDENT), {})
        self.assertEqual(world.reads, [], "the notifier's inline path read state")
        homeserver.clock.run_pending()
        self.assertNotEqual(world.reads, [])

    async def test_a_refused_job_does_not_look_its_courses_up(self) -> None:
        world = _classroom()
        module, _api, homeserver, _db = self._module(world)
        assert module._dispatcher is not None
        with patch.object(module._dispatcher, "enqueue", return_value=False):
            await module.on_new_event(_event("hello", sender=STUDENT), {})
        homeserver.clock.run_pending()
        self.assertEqual(world.reads, [], "a refused job still read state")

    async def test_a_failed_snapshot_is_looked_up_at_the_verdict(self) -> None:
        world = _classroom()
        module, _api, homeserver, _db = self._module(world)
        assert module._dispatcher is not None
        queued: List[ModerationJob] = []
        with patch.object(
            module._dispatcher,
            "enqueue",
            side_effect=lambda job: queued.append(job) or True,
        ):
            await module.on_new_event(_event("you are awful", sender=STUDENT), {})
        world.error = RuntimeError("down at queue time")
        homeserver.clock.run_pending()
        world.error = None
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(queued[0])
        self.assertEqual((await self._row(module, "$evt1")).course_ids, (COURSE_A,))


# ---------------------------------------------------------------------------
# What reaches a log
# ---------------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestNoTextInLogs(Tier2Case):
    SECRET = "zebra-secret-text-0147"

    def _assert_absent(self, capture: _Capture) -> None:
        self.assertTrue(capture.records, "nothing was logged, so nothing was checked")
        for record in capture.records:
            rendered = " ".join(
                [record.getMessage(), repr(record.args), repr(vars(record))]
            )
            self.assertNotIn(self.SECRET, rendered)

    async def test_failing_writes_never_log_the_text(self) -> None:
        capture = _Capture()
        root = logging.getLogger()
        root.addHandler(capture)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, capture)
        self.addCleanup(root.setLevel, old_level)

        module, _api, homeserver, db_pool = self._module()
        db_pool.error = RuntimeError(f"driver quoting {self.SECRET}")
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job(text=self.SECRET))
        for seconds in (1, 2, 4, 8, 16):
            homeserver.clock.advance(seconds)

        tier1, hs1, db1 = _tier1(_classroom())
        db1.error = RuntimeError(f"driver quoting {self.SECRET}")
        await tier1.check_event_for_spam(
            _event(f"415-555-2671 {self.SECRET}", sender=STUDENT)
        )
        for seconds in (1, 2, 4, 8, 16):
            hs1.clock.advance(seconds)
        self._assert_absent(capture)


# ---------------------------------------------------------------------------
# The recorder's retry, directly
# ---------------------------------------------------------------------------


class TestRecorderRetry(unittest.IsolatedAsyncioTestCase):
    async def test_a_verdict_retry_drops_the_outcome_it_carried(self) -> None:
        world = _classroom()
        store, homeserver = _store()
        recorder = IncidentRecorder(homeserver, store, world.courses())
        homeserver.store.db_pool.error = RuntimeError("down")
        written, _prior = await recorder.record_verdict(
            _incident(action=ACTION_REDACTED, outcome=OUTCOME_PENDING, course_ids=None)
        )
        self.assertFalse(written)
        homeserver.store.db_pool.error = None
        homeserver.clock.advance(1.0)
        row = await store.get("mod:$e1")
        assert row is not None
        self.assertEqual((row.action, row.outcome), (ACTION_REDACTED, None))
        self.assertEqual(row.course_ids, (COURSE_A,))
