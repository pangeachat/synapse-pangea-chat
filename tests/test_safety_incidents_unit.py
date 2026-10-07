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
from twisted.internet import defer

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
    FakeEvent,
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
        room_version=SimpleNamespace(
            msc4289_creator_power_enabled=False, implicit_room_creator=True
        ),
    )


class CourseWorld:
    """Rooms, their course plans and power levels, and a TIMELINE of who
    joined and left where - so a test can ask what was true at a position.

    Every change takes the next stream position. "The learner leaves before
    the verdict" is a test that changes the world after the incident's
    position; the course rule must still answer as of that position.
    """

    def __init__(self) -> None:
        self.plans: Dict[str, bool] = {}
        self.levels: Dict[str, Dict[str, int]] = {}
        self.created: Dict[str, int] = {}
        self.timeline: List[Tuple[int, str, str, bool]] = []
        self.position = 0
        self.reads: List[Tuple[str, str]] = []
        self.error: Optional[Exception] = None

    def _tick(self) -> int:
        self.position += 1
        return self.position

    def room(self, room_id: str, *, course: bool, **levels: int) -> None:
        self.plans[room_id] = course
        self.levels[room_id] = {f"@{k}:example.org": v for k, v in levels.items()}
        self.created.setdefault(room_id, self._tick())

    def join(self, user_id: str, room_id: str) -> None:
        self.timeline.append((self._tick(), user_id, room_id, True))

    def leave(self, user_id: str, room_id: str) -> None:
        self.timeline.append((self._tick(), user_id, room_id, False))

    def joined_at(self, user_id: str, room_id: str, position: int) -> bool:
        joined = False
        for at, user, room, is_join in self.timeline:
            if at <= position and user == user_id and room == room_id:
                joined = is_join
        return joined

    @property
    def joined(self) -> Dict[str, Set[str]]:
        """Who is joined where NOW."""
        result: Dict[str, Set[str]] = {}
        for _at, user, room, _join in self.timeline:
            if self.joined_at(user, room, self.position):
                result.setdefault(user, set()).add(room)
        return result

    async def rooms_ever_joined(self, user_id: str) -> Set[str]:
        if self.error is not None:
            raise self.error
        return {
            room
            for _at, user, room, is_join in self.timeline
            if user == user_id and is_join
        }

    async def is_course_now(self, room_id: str) -> bool:
        return bool(self.plans.get(room_id))

    async def state_at(
        self, room_id: str, user_id: str, position: int
    ) -> Dict[Any, Any]:
        self.reads.append((room_id, user_id))
        if self.error is not None:
            raise self.error
        if self.created.get(room_id, 10**9) > position:
            return {}
        state: Dict[Any, Any] = {
            (CREATE_EVENT_TYPE, ""): _state_event({"type": "m.space"}),
            (POWER_LEVELS_EVENT_TYPE, ""): _state_event(
                {"users": dict(self.levels.get(room_id, {}))}
            ),
        }
        if self.plans.get(room_id):
            state[(COURSE_PLAN_EVENT_TYPE, "")] = _state_event({"uuid": "plan"})
        if self.joined_at(user_id, room_id, position):
            state[(MEMBER_EVENT_TYPE, user_id)] = _state_event(
                {"membership": "join"}, sender=user_id
            )
        return state

    async def read_state(self, room_id: str, user_id: str) -> Dict[Any, Any]:
        """Current state, for the read endpoint's admin check."""
        return await self.state_at(room_id, user_id, self.position)

    def courses(
        self, is_exempt: Callable[[str], bool] = lambda _user: False
    ) -> StudentCourses:
        return StudentCourses(
            rooms_ever_joined=self.rooms_ever_joined,
            is_course_now=self.is_course_now,
            state_at=self.state_at,
            current_position=lambda: self.position,
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
        await store.insert(_incident(outcome=OUTCOME_PENDING, attempt_id="a"))
        self.assertFalse(
            await store.set_outcome("mod:$e1", OUTCOME_UNKNOWN, attempt_id="b"),
            "a result from an attempt that does not own the row was written",
        )
        self.assertFalse(
            await store.set_outcome("mod:$e1", OUTCOME_UNKNOWN, only_if=OUTCOME_FAILED)
        )
        self.assertTrue(
            await store.set_outcome(
                "mod:$e1", OUTCOME_UNKNOWN, attempt_id="a", only_if=OUTCOME_PENDING
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
        self.assertEqual(await courses.for_user(STUDENT, world.position), (COURSE_A,))
        self.assertEqual(
            await courses.for_user(TEACHER, world.position),
            (),
            "a course admin is nobody's student",
        )

    async def test_a_room_without_a_course_plan_is_not_a_course(self) -> None:
        world = _classroom()
        world.room(ROOM, course=False)
        self.assertNotIn(ROOM, await world.courses().for_user(STUDENT, world.position))

    async def test_exempt_users_and_bots_are_nobodys_students(self) -> None:
        world = _classroom()
        bot = "@bot:example.org"
        world.join(bot, COURSE_A)
        exempt = world.courses(is_exempt=lambda user: user == STUDENT)
        self.assertEqual(await exempt.for_user(STUDENT, world.position), ())
        self.assertEqual(await world.courses().for_user(bot, world.position), ())

    async def test_a_read_that_fails_is_an_error_and_not_a_student_nowhere(
        self,
    ) -> None:
        world = _classroom()
        world.error = RuntimeError("store down")
        with self.assertRaises(RuntimeError):
            await world.courses().for_user(STUDENT, world.position)

    async def test_a_teachers_report_reaches_only_the_learners_course(self) -> None:
        """The teacher teaches A and B; the learner is in A. A report the
        teacher files about the learner must reach A only - B's admins have
        no business seeing a message from a learner who is not theirs."""
        world = _classroom()
        self.assertEqual(
            await world.courses().for_report(STUDENT, TEACHER, world.position),
            (COURSE_A,),
        )

    async def test_a_reporters_own_student_courses_are_not_added(self) -> None:
        """The reporter is a LEARNER in another course too. Their own
        courses are a fallback for a subject who is a student nowhere, never
        an addition: course C's admins must not see a message from course A."""
        world = _classroom()
        course_c = "!course-c:example.org"
        world.room(course_c, course=True, someone=100)
        world.join(TEACHER, course_c)
        self.assertEqual(
            await world.courses().for_user(TEACHER, world.position), (course_c,)
        )
        self.assertEqual(
            await world.courses().for_report(STUDENT, TEACHER, world.position),
            (COURSE_A,),
        )

    async def test_a_report_about_an_outsider_goes_to_the_reporters_courses(
        self,
    ) -> None:
        world = _classroom()
        world.join(OUTSIDER, ROOM)
        self.assertEqual(
            await world.courses().for_report(OUTSIDER, STUDENT, world.position),
            (COURSE_A,),
        )

    async def test_nobodys_report_reaches_no_course(self) -> None:
        world = _classroom()
        self.assertEqual(
            await world.courses().for_report(OUTSIDER, TEACHER, world.position), ()
        )

    async def test_courses_are_read_as_of_the_position_not_now(self) -> None:
        """The learner was in A at the incident, then left A and joined B as
        a learner. Asked at the incident's position, the answer is A - and
        only A - however late the question is asked."""
        world = _classroom()
        at_incident = world.position
        world.leave(STUDENT, COURSE_A)
        course_c = "!course-c:example.org"
        world.room(course_c, course=True, teacher=100)
        world.join(STUDENT, COURSE_B)
        world.join(STUDENT, course_c)
        courses = world.courses()
        self.assertEqual(await courses.for_user(STUDENT, at_incident), (COURSE_A,))
        self.assertEqual(
            await courses.for_user(STUDENT, world.position), (COURSE_B, course_c)
        )

    async def test_a_creator_with_no_power_levels_event_is_an_admin(self) -> None:
        """No `m.room.power_levels` in the room: the spec gives the creator
        100 in every room version, so the creator is a course admin, not a
        student - in a v11 room as much as in a v12 one."""
        create = SimpleNamespace(
            content={"type": "m.space"},
            sender=TEACHER,
            room_version=SimpleNamespace(
                msc4289_creator_power_enabled=False, implicit_room_creator=True
            ),
        )
        state = {
            (CREATE_EVENT_TYPE, ""): create,
            (COURSE_PLAN_EVENT_TYPE, ""): _state_event({"uuid": "plan"}),
            (MEMBER_EVENT_TYPE, TEACHER): _state_event({"membership": "join"}),
            (MEMBER_EVENT_TYPE, STUDENT): _state_event({"membership": "join"}),
        }
        self.assertTrue(is_course_admin_in(state, TEACHER))
        self.assertFalse(is_student_in(state, TEACHER))
        self.assertTrue(is_student_in(state, STUDENT))

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


def _accepting(queued: List[ModerationJob]) -> Callable[[ModerationJob], bool]:
    """`Tier2Dispatcher.enqueue`, accepting every job into `queued`."""

    def _enqueue(job: ModerationJob) -> bool:
        queued.append(job)
        return True

    return _enqueue


def _sent_event(world: CourseWorld, body: str, event_id: str = "$e1") -> Any:
    """A message as `on_new_event` sees it: persisted, with its stream
    position set - the next one in the world's timeline."""
    event = FakeEvent(body, sender=STUDENT, event_id=event_id)
    world.position += 1
    event.internal_metadata = SimpleNamespace(stream_ordering=world.position)
    return event


class TestCourseIsFrozenAtQueueTime(Tier2Case):
    async def _queue(self, module: ChatModeration, event: Any) -> ModerationJob:
        assert module._dispatcher is not None
        queued: List[ModerationJob] = []
        with patch.object(
            module._dispatcher,
            "enqueue",
            side_effect=_accepting(queued),
        ):
            await module.on_new_event(event, {})
        self.assertEqual(len(queued), 1)
        return queued[0]

    async def test_a_learner_who_leaves_before_the_verdict_lands_on_the_course(
        self,
    ) -> None:
        world = _classroom()
        module, _api, _hs, _db = self._module(world)
        job = await self._queue(module, _sent_event(world, "you are awful"))
        self.assertEqual(job.position, world.position)
        world.leave(STUDENT, COURSE_A)
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(job)
        self.assertEqual(
            (await self._row(module)).course_ids,
            (COURSE_A,),
            "the course was read at verdict time, after the learner had left",
        )

    async def test_leaving_and_joining_another_course_before_the_read(self) -> None:
        """The window a lookup scheduled for later cannot close: the learner
        leaves A and joins B between the queue and ANY read. The answer must
        still be A, and only A."""
        world = _classroom()
        module, _api, _hs, _db = self._module(world)
        job = await self._queue(module, _sent_event(world, "you are awful"))
        world.leave(STUDENT, COURSE_A)
        world.join(STUDENT, COURSE_B)
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(job)
        self.assertEqual((await self._row(module)).course_ids, (COURSE_A,))

    async def test_a_lookup_that_fails_is_retried_at_the_same_position(self) -> None:
        world = _classroom()
        module, api, homeserver, _db = self._module(world)
        job = await self._queue(module, _sent_event(world, "you are awful"))
        world.error = RuntimeError("state unreadable")
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(job)
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        world.error = None
        world.leave(STUDENT, COURSE_A)
        world.join(STUDENT, COURSE_B)
        homeserver.clock.advance(1.0)
        row = await self._row(module)
        self.assertEqual(row.course_ids, (COURSE_A,))
        self.assertEqual((row.action, row.outcome), (ACTION_REDACTED, None))

    async def test_on_new_event_reads_no_state(self) -> None:
        world = _classroom()
        module, _api, homeserver, _db = self._module(world)
        await self._queue(module, _sent_event(world, "hello"))
        homeserver.clock.run_pending()
        self.assertEqual(world.reads, [], "queueing a message read state")


class TestABlockKeepsTheCoursesItResolved(unittest.IsolatedAsyncioTestCase):
    async def test_a_retry_writes_the_courses_of_the_block(self) -> None:
        world = _classroom()
        module, homeserver, db_pool = _tier1(world)
        refuse = {"on": True}

        def _refuse_incidents(sql: str, _args: Any) -> None:
            if refuse["on"] and sql.lstrip().startswith("INSERT INTO pangea_safety"):
                raise RuntimeError("insert refused")

        db_pool.on_statement = _refuse_incidents
        tier1_refusal_code(
            await module.check_event_for_spam(_event(PHONE_TEXT, sender=STUDENT))
        )
        world.leave(STUDENT, COURSE_A)
        world.join(STUDENT, COURSE_B)
        refuse["on"] = False
        homeserver.clock.advance(1.0)
        self.assertEqual(_rows(db_pool)[0][3], f'["{COURSE_A}"]')


class TestACancellationDoesNotLoseTheFinding(Tier2Case):
    async def test_cancelled_while_preserving(self) -> None:
        module, _api, homeserver, _db = self._module()

        async def _cancelled(job: Any, category: str) -> None:
            raise defer.CancelledError()

        with patch.object(module, "_record_preserved", _cancelled):
            with patch(MODERATE_TEXT, _verdict("self-harm/intent")):
                with self.assertRaises(defer.CancelledError):
                    await module._check_and_redact(_job())
        homeserver.clock.advance(1.0)
        row = await self._row(module)
        self.assertEqual(row.action, ACTION_PRESERVED)
        self.assertTrue(row.self_harm)

    async def test_cancelled_while_flushing_before_a_redaction(self) -> None:
        module, api, homeserver, _db = self._module()
        assert module._disposition is not None

        async def _cancelled() -> None:
            raise defer.CancelledError()

        with patch.object(module._disposition, "flush_pending", _cancelled):
            with patch(MODERATE_TEXT, _verdict("harassment")):
                with self.assertRaises(defer.CancelledError):
                    await module._check_and_redact(_job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        homeserver.clock.advance(1.0)
        row = await self._row(module)
        self.assertEqual((row.action, row.outcome), (ACTION_REDACTED, None))


class TestAnUnconfirmedScreenIsStillRecorded(Tier2Case):
    async def _screened(self, module: ChatModeration, confirmation: Any) -> None:
        assert module._checker is not None
        screen = {
            "flagged": True,
            "categories": ["self-harm/intent"],
            "evaluated": True,
        }
        with patch.object(
            module._checker, "check", AsyncMock(return_value=confirmation)
        ):
            await module._decide_screened(_job(), screen, False)

    async def test_no_confirmation_records_the_screen_as_left_up(self) -> None:
        module, api, _hs, _db = self._module()
        await self._screened(module, None)
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        row = await self._row(module)
        self.assertEqual((row.action, row.outcome), (ACTION_KEPT, None))
        self.assertTrue(row.self_harm)

    async def test_a_clean_confirmation_is_the_verdict(self) -> None:
        module, _api, _hs, _db = self._module()
        await self._screened(
            module, {"flagged": False, "categories": [], "evaluated": True}
        )
        self.assertIsNone(await module._incidents.get("mod:$e1"))


class TestAttemptsDoNotOverwriteEachOther(unittest.IsolatedAsyncioTestCase):
    async def test_a_stale_result_cannot_replace_a_later_attempts(self) -> None:
        store, _hs = _store()
        await store.upsert_verdict(
            _incident(action=ACTION_REDACTED, outcome=OUTCOME_PENDING, attempt_id="a")
        )
        await store.upsert_verdict(
            _incident(action=ACTION_REDACTED, outcome=OUTCOME_PENDING, attempt_id="b")
        )
        self.assertTrue(
            await store.set_outcome("mod:$e1", OUTCOME_UNKNOWN, attempt_id="b")
        )
        self.assertFalse(
            await store.set_outcome("mod:$e1", OUTCOME_FAILED, attempt_id="a"),
            "attempt a's late result overwrote attempt b's",
        )
        row = await store.get("mod:$e1")
        assert row is not None
        self.assertEqual(row.outcome, OUTCOME_UNKNOWN)

    async def test_a_new_attempt_moves_updated_ms_even_when_still_pending(self) -> None:
        store, _hs = _store()
        await store.insert(
            _incident(
                action=ACTION_REDACTED,
                outcome=OUTCOME_PENDING,
                attempt_id="crashed",
                updated_ms=1,
            )
        )
        await store.upsert_verdict(
            _incident(
                action=ACTION_REDACTED, outcome=OUTCOME_PENDING, attempt_id="live"
            )
        )
        row = await store.get("mod:$e1")
        assert row is not None
        self.assertEqual(row.attempt_id, "live")
        self.assertGreater(row.updated_ms, 1)


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


class _RealLoggingPool(DbPoolDouble):
    """`db_pool` over SQLite with Synapse's REAL `LoggingTransaction`, which
    logs every statement's arguments to `synapse.storage.SQL` at DEBUG. The
    double the other tests use does not log, so it cannot see this channel."""

    async def runInteraction(
        self, desc: str, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        from synapse.storage.database import LoggingTransaction

        txn = LoggingTransaction(
            txn=self.connection.cursor(),
            name=desc,
            server_name="example.org",
            database_engine=sqlite_engine(),
        )
        result = func(txn, *args, **kwargs)
        self.connection.commit()
        return result


class TestNoTextInSynapsesSqlLog(unittest.IsolatedAsyncioTestCase):
    SECRET_TEXT = "quokka-text-5511"
    SECRET_REASON = "quokka-reason-5522"

    async def test_text_and_reason_never_reach_the_sql_debug_log(self) -> None:
        pool = _RealLoggingPool()
        self.addCleanup(pool.connection.close)
        store_double = EventStoreDouble()
        homeserver = HomeServerDouble(store_double)
        store_double.db_pool = pool
        store = IncidentStore(homeserver)
        capture = _Capture()
        sql_logger = logging.getLogger("synapse.storage.SQL")
        sql_logger.addHandler(capture)
        old = sql_logger.level
        sql_logger.setLevel(logging.DEBUG)
        self.addCleanup(sql_logger.removeHandler, capture)
        self.addCleanup(sql_logger.setLevel, old)

        await store.insert(
            _incident(
                incident_id="report:r",
                text=self.SECRET_TEXT,
                reason=self.SECRET_REASON,
            )
        )
        await store.upsert_verdict(_incident(text=self.SECRET_TEXT))
        await store.upsert_verdict(
            _incident(text=self.SECRET_TEXT, action=ACTION_PRESERVED, self_harm=True)
        )
        await store.set_outcome("mod:$e1", OUTCOME_UNKNOWN)
        row = await store.get("report:r")
        assert row is not None
        self.assertEqual((row.text, row.reason), (self.SECRET_TEXT, self.SECRET_REASON))

        self.assertTrue(
            any("[SQL values]" in r.getMessage() for r in capture.records),
            "the SQL debug log captured nothing, so the check is not live",
        )
        for record in capture.records:
            self.assertNotIn(self.SECRET_TEXT, record.getMessage())
            self.assertNotIn(self.SECRET_REASON, record.getMessage())


class TestRoundTwoFindings(Tier2Case):
    """Each test here is a defect a cross-model review reproduced."""

    async def test_an_absurd_score_never_costs_the_self_harm_protection(self) -> None:
        module, api, _hs, _db = self._module()
        with patch(
            MODERATE_TEXT,
            _verdict("self-harm/intent", scores={"self-harm/intent": 10**400}),
        ):
            await module._check_and_redact(_job())
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        row = await self._row(module)
        self.assertEqual(row.action, ACTION_PRESERVED)
        self.assertIsNone(row.top_score)

    async def test_a_declined_repeat_gives_ownership_back(self) -> None:
        """Attempt A's send raised and its result is still on its way; a
        second verdict starts attempt B, whose claim is declined. B must put
        back A's outcome AND A's ownership, or A's result can never land."""
        store, _hs = _store()
        await store.upsert_verdict(
            _incident(action=ACTION_REDACTED, outcome=OUTCOME_PENDING, attempt_id="a")
        )
        await store.upsert_verdict(
            _incident(action=ACTION_REDACTED, outcome=OUTCOME_PENDING, attempt_id="b")
        )
        self.assertTrue(await store.abandon_attempt("mod:$e1", "b"))
        self.assertTrue(
            await store.set_outcome("mod:$e1", OUTCOME_UNKNOWN, attempt_id="a"),
            "attempt a's result could not land after b declined",
        )
        row = await store.get("mod:$e1")
        assert row is not None
        self.assertEqual(row.outcome, OUTCOME_UNKNOWN)

    async def test_abandoning_a_first_attempt_is_skipped_and_removed_is_final(
        self,
    ) -> None:
        store, _hs = _store()
        await store.upsert_verdict(
            _incident(action=ACTION_REDACTED, outcome=OUTCOME_PENDING, attempt_id="a")
        )
        self.assertTrue(await store.abandon_attempt("mod:$e1", "a"))
        row = await store.get("mod:$e1")
        assert row is not None
        self.assertEqual(row.outcome, OUTCOME_SKIPPED)
        await store.insert(
            _incident(incident_id="mod:$gone", outcome=OUTCOME_REMOVED, attempt_id="x")
        )
        self.assertFalse(await store.abandon_attempt("mod:$gone", "x"))

    async def test_cancelled_after_the_pending_row_committed(self) -> None:
        """The upsert commits, then the coroutine is cancelled at the re-read.
        Nothing will be sent, so the row must not stay `pending`."""
        module, api, homeserver, _db = self._module()

        async def _cancelled(job: Any) -> None:
            raise defer.CancelledError()

        with patch.object(module, "_redaction_blocker", _cancelled):
            with patch(MODERATE_TEXT, _verdict("harassment")):
                with self.assertRaises(defer.CancelledError):
                    await module._check_and_redact(_job())
        homeserver.clock.advance(1.0)
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        row = await self._row(module)
        self.assertEqual((row.action, row.outcome), (ACTION_REDACTED, OUTCOME_SKIPPED))

    async def test_a_declined_claim_restores_an_earlier_failure(self) -> None:
        module, api, _hs, _db = self._module()
        cast(AsyncMock, api.create_and_send_event_into_room).side_effect = RuntimeError(
            "send refused"
        )
        with patch(MODERATE_TEXT, _verdict("harassment")):
            await module._check_and_redact(_job())
        assert module._dispatcher is not None
        with patch.object(
            type(module._dispatcher),
            "actions_permitted",
            new_callable=lambda: property(lambda _self: False),
        ):
            with patch(MODERATE_TEXT, _verdict("harassment")):
                await module._check_and_redact(_job())
        row = await self._row(module)
        self.assertEqual(row.outcome, OUTCOME_FAILED)

    async def test_a_cancelled_confirmation_keeps_the_screen(self) -> None:
        module, _api, homeserver, _db = self._module()
        assert module._checker is not None
        screen = {
            "flagged": True,
            "categories": ["self-harm/intent"],
            "evaluated": True,
        }

        async def _cancelled(text: str) -> Any:
            raise defer.CancelledError()

        with patch.object(module._checker, "check", _cancelled):
            with self.assertRaises(defer.CancelledError):
                await module._decide_screened(_job(), screen, False)
        homeserver.clock.advance(1.0)
        row = await self._row(module)
        self.assertEqual(row.action, ACTION_KEPT)
        self.assertTrue(row.self_harm)

    async def test_a_cancelled_batch_hands_on_the_answers_it_did_not_reach(
        self,
    ) -> None:
        module, _api, homeserver, _db = self._module()
        jobs = (_job("$e1"), _job("$e2"), _job("$e3"))
        results = [
            {"flagged": True, "categories": ["harassment"], "evaluated": True},
            {"flagged": True, "categories": ["self-harm/intent"], "evaluated": True},
            {"flagged": False, "categories": [], "evaluated": True},
        ]
        assert module._checker is not None
        batch = SimpleNamespace(results=results, confirmed=True)

        async def _check_batch(texts: Any) -> Any:
            return batch

        async def _decide(job: Any, result: Any) -> None:
            raise defer.CancelledError()

        with patch.object(module._checker, "check_batch", _check_batch):
            with patch.object(module, "_decide", _decide):
                with self.assertRaises(defer.CancelledError):
                    await module._screen_batch(jobs)
        homeserver.clock.advance(1.0)
        second = await self._row(module, "$e2")
        self.assertEqual(second.action, ACTION_KEPT)
        self.assertTrue(second.self_harm)
        self.assertIsNone(await module._incidents.get("mod:$e3"))


class TestCreatorsByRoomVersion(unittest.TestCase):
    def test_an_explicit_creator_is_the_creator(self) -> None:
        """Before MSC4289 a room's creator is `content.creator`, which need
        not be the create event's sender. Synapse's rule decides."""
        create = SimpleNamespace(
            content={"creator": TEACHER, "type": "m.space"},
            sender=STUDENT,
            room_version=SimpleNamespace(
                msc4289_creator_power_enabled=False, implicit_room_creator=False
            ),
        )
        state = {
            (CREATE_EVENT_TYPE, ""): create,
            (COURSE_PLAN_EVENT_TYPE, ""): _state_event({"uuid": "plan"}),
            (MEMBER_EVENT_TYPE, TEACHER): _state_event({"membership": "join"}),
            (MEMBER_EVENT_TYPE, STUDENT): _state_event({"membership": "join"}),
        }
        self.assertTrue(is_course_admin_in(state, TEACHER))
        self.assertFalse(is_course_admin_in(state, STUDENT))
        self.assertTrue(is_student_in(state, STUDENT))


class TestAFailedWriteNeverCarriesTheRow(unittest.IsolatedAsyncioTestCase):
    SECRET = "wombat-text-7731"

    async def test_the_drivers_message_is_replaced_by_its_type(self) -> None:
        import sqlite3

        store, homeserver = _store()
        db_pool = homeserver.store.db_pool

        def _refuse(sql: str, args: Any) -> None:
            if sql.lstrip().startswith("INSERT INTO pangea_safety_incidents"):
                raise sqlite3.IntegrityError(f"Failing row contains ({self.SECRET})")

        db_pool.on_statement = _refuse
        with self.assertRaises(Exception) as caught:
            await store.insert(_incident(text=self.SECRET))
        error = caught.exception
        self.assertNotIn(self.SECRET, str(error))
        self.assertNotIn(self.SECRET, repr(error))
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertIn("IntegrityError", str(error))

    async def test_a_retryable_failure_is_passed_through_for_synapse(self) -> None:
        import sqlite3

        store, homeserver = _store()

        def _locked(sql: str, args: Any) -> None:
            if sql.lstrip().startswith("INSERT INTO pangea_safety_incidents"):
                raise sqlite3.OperationalError("database is locked")

        homeserver.store.db_pool.on_statement = _locked
        with self.assertRaises(sqlite3.OperationalError):
            await store.insert(_incident())


class TestCandidatesWithoutCurrentState(unittest.IsolatedAsyncioTestCase):
    async def test_a_room_with_no_current_state_stays_a_candidate(self) -> None:
        """Synapse drops a room's current state when its last local member
        leaves. That says nothing about whether the room was a course at the
        incident's position, so it must not exclude it."""
        from synapse_pangea_chat.moderation import courses as courses_module

        answers: Dict[str, Dict[Any, str]] = {
            "!gone:example.org": {},
            "!course:example.org": {
                (CREATE_EVENT_TYPE, ""): "$c",
                (COURSE_PLAN_EVENT_TYPE, ""): "$p",
            },
            "!chat:example.org": {(CREATE_EVENT_TYPE, ""): "$c"},
        }

        async def _current(room_id: str, state_filter: Any) -> Dict[Any, str]:
            return answers[room_id]

        homeserver = SimpleNamespace(
            get_storage_controllers=lambda: SimpleNamespace(
                state=SimpleNamespace(get_current_state_ids=_current)
            )
        )
        check = courses_module._is_course_now(homeserver)
        self.assertTrue(await check("!gone:example.org"))
        self.assertTrue(await check("!course:example.org"))
        self.assertFalse(await check("!chat:example.org"))
