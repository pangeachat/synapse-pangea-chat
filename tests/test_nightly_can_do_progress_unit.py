"""Unit tests for the nightly Can-Do progress pass's decisions
(scripts/nightly_can_do_progress.py and its docstring)."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, Iterator, List, Optional
from unittest import mock

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "nightly_can_do_progress.py",
)
_spec = importlib.util.spec_from_file_location("nightly_can_do_progress", _SCRIPT)
assert _spec is not None and _spec.loader is not None
nightly: Any = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = nightly
_spec.loader.exec_module(nightly)

TOKEN = "syt_secret_admin_token_value"
PASSWORD = "bot-password-value"
ROOM = "!analyticsroomid:staging.pangea.chat"
COURSE = "!courseroomid:staging.pangea.chat"
LEARNER = "@learner_name:staging.pangea.chat"
NOTE = "Teacher's private note about the unit"
SENSITIVE = (TOKEN, PASSWORD, ROOM, COURSE, LEARNER, NOTE, "staging.pangea.chat:")
STAGING = nightly.ENVIRONMENTS["staging"]
PROD = nightly.ENVIRONMENTS["prod"]
BEFORE_END = datetime.date(2026, 11, 20)


def _record(outcome: str) -> Dict[str, Any]:
    return {
        "room_id": ROOM,
        "course_id": COURSE,
        "user_id": LEARNER,
        "outcome": outcome,
        "keys_moved": 2 if outcome == "rewrite" else 0,
        "completions_kept": 1 if outcome == "rewrite" else 0,
    }


def _records(**outcomes: int) -> List[Dict[str, Any]]:
    return [_record(o) for o, n in outcomes.items() for _ in range(n)]


def _space(
    status: int = 200, writes: bool = False, old_ids_left: int = 0
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "writes": [{"room_id": COURSE}] if writes else [],
        "course_missions": {"notes_trimmed": [{"dropped": NOTE}]},
    }
    if old_ids_left:
        body["course_missions"] = {
            "outcome": "ignored_by_readers",
            "reason": f"{COURSE} due_at",
            "old_ids_left": old_ids_left,
        }
    if status != 200:
        body = {"error": f"refused for {COURSE}"}
    return {"room_id": COURSE, "status": status, "body": body}


class FakeHomeserver:
    """Answers the night's calls from scripted progress runs."""

    def __init__(
        self,
        progress_runs: List[Any],
        spaces: Optional[List[Dict[str, Any]]] = None,
        spaces_error: Optional[Exception] = None,
    ) -> None:
        self.progress_runs = list(progress_runs)
        self.spaces = spaces if spaces is not None else [_space(), _space(409)]
        self.spaces_error = spaces_error
        self.calls: List[str] = []

    def space_dry_run(self) -> Dict[str, Any]:
        self.calls.append("spaces")
        if self.spaces_error:
            raise self.spaces_error
        return {"spaces": self.spaces}

    def progress(self, dry_run: bool) -> List[Dict[str, Any]]:
        self.calls.append("dry run" if dry_run else "apply")
        run = self.progress_runs.pop(0)
        if isinstance(run, Exception):
            raise run
        return run

    def steps(self) -> Any:
        return nightly.Steps(space_dry_run=self.space_dry_run, progress=self.progress)


def _main(
    fake: Optional[FakeHomeserver],
    today: datetime.date = BEFORE_END,
    make_steps: Any = None,
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    with tempfile.NamedTemporaryFile("r", suffix=".md") as summary:
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summary.name}):
            with contextlib.redirect_stdout(stdout):
                code = nightly.main(
                    ["--environment", "staging"],
                    today=today,
                    make_steps=make_steps or (lambda env: fake.steps()),  # type: ignore[union-attr]
                )
        return code, stdout.getvalue(), summary.read()


class TestDecisions(unittest.TestCase):
    def test_nothing_to_rewrite_is_not_applied(self) -> None:
        fake = FakeHomeserver([_records(unchanged=5, not_local=1)])
        code, out, _ = _main(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, ["spaces", "dry run"])
        self.assertIn("nothing to rewrite; not applied", out)

    def test_records_to_rewrite_are_applied_then_checked_again(self) -> None:
        fake = FakeHomeserver(
            [
                _records(rewrite=1, unchanged=210),
                _records(rewrite=1, unchanged=210),
                _records(unchanged=211),
            ]
        )
        code, out, summary = _main(fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.calls, ["spaces", "dry run", "apply", "dry run"])
        self.assertIn("progress apply: rewrite 1", out)
        self.assertIn("ok", summary.splitlines()[0])

    def test_over_the_failure_limit_is_not_applied(self) -> None:
        bad = nightly.FAILURE_LIMIT + 1
        fake = FakeHomeserver([_records(rewrite=3, failed=bad - 2, refused=2)])
        code, out, _ = _main(fake)
        self.assertEqual(code, 1)
        self.assertNotIn("apply", fake.calls)
        self.assertIn(f"{bad} failed or refused records in the dry run", out)

    def test_over_the_failure_limit_fails_even_with_nothing_to_rewrite(self) -> None:
        fake = FakeHomeserver([_records(unchanged=3, failed=nightly.FAILURE_LIMIT + 1)])
        code, _, _ = _main(fake)
        self.assertEqual(code, 1)
        self.assertNotIn("apply", fake.calls)

    def test_at_the_failure_limit_is_applied(self) -> None:
        limit = nightly.FAILURE_LIMIT
        fake = FakeHomeserver(
            [
                _records(rewrite=1, failed=limit),
                _records(rewrite=1, failed=limit),
                _records(unchanged=1, failed=limit),
            ]
        )
        code, _, _ = _main(fake)
        self.assertEqual(code, 0)
        self.assertIn("apply", fake.calls)

    def test_unknown_outcome_counts_as_failed(self) -> None:
        counts = nightly.progress_counts(_records(rewrite=1, something_new=2))
        self.assertEqual(counts["failed"], 2)
        self.assertNotIn("something_new", counts)

    def test_second_dry_run_not_clean_fails(self) -> None:
        fake = FakeHomeserver(
            [_records(rewrite=2), _records(rewrite=2), _records(rewrite=1, unchanged=1)]
        )
        code, out, summary = _main(fake)
        self.assertEqual(code, 1)
        self.assertIn("second dry run still finds 1 records to rewrite", out)
        self.assertIn("needs a person", summary)

    def test_failed_apply_still_runs_the_second_dry_run_and_fails(self) -> None:
        fake = FakeHomeserver(
            [
                _records(rewrite=2),
                RuntimeError(f"page after {ROOM}"),
                _records(rewrite=2),
            ]
        )
        code, out, _ = _main(fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, ["spaces", "dry run", "apply", "dry run"])
        self.assertIn("progress apply failed (RuntimeError)", out)

    def test_apply_over_the_failure_limit_fails(self) -> None:
        fake = FakeHomeserver(
            [
                _records(rewrite=7),
                _records(refused=nightly.FAILURE_LIMIT + 1, rewrite=1),
                _records(unchanged=7),
            ]
        )
        code, _, _ = _main(fake)
        self.assertEqual(code, 1)

    def test_failed_progress_dry_run_fails_without_applying(self) -> None:
        fake = FakeHomeserver([RuntimeError(f"A page failed after {ROOM}")])
        code, out, _ = _main(fake)
        self.assertEqual(code, 1)
        self.assertNotIn("apply", fake.calls)
        self.assertIn("progress dry run failed (RuntimeError)", out)

    def test_space_left_to_rewrite_needs_a_person_and_is_never_applied(self) -> None:
        fake = FakeHomeserver([_records(unchanged=1)], spaces=[_space(writes=True)])
        code, out, _ = _main(fake)
        self.assertEqual(code, 1)
        self.assertIn("1 course spaces still name old Can-Dos", out)

    def test_refused_space_is_counted_not_failed(self) -> None:
        fake = FakeHomeserver([_records(unchanged=1)], spaces=[_space(409)])
        code, out, _ = _main(fake)
        self.assertEqual(code, 0, out)
        self.assertIn("refused_no_local_instructor 1", out)

    def test_failed_space_and_failed_space_walk_need_a_person(self) -> None:
        for fake in (
            FakeHomeserver([_records(unchanged=1)], spaces=[_space(500)]),
            FakeHomeserver(
                [_records(unchanged=1)], spaces_error=RuntimeError(f"HTTP 500 {COURSE}")
            ),
        ):
            code, _, _ = _main(fake)
            self.assertEqual(code, 1)
            # A failed space walk does not stop the progress pass.
            self.assertIn("dry run", fake.calls)

    def test_failed_token_mint_fails(self) -> None:
        def boom(env: Any) -> Any:
            raise RuntimeError(f"login refused for {LEARNER} with {PASSWORD}")

        code, out, _ = _main(None, make_steps=boom)
        self.assertEqual(code, 1)
        self.assertIn("minting the admin token failed (RuntimeError)", out)


class TestUnresolved(unittest.TestCase):
    """Audit finding 5: records and spaces left undone fail the night over
    the limit, whether or not anything was applied."""

    def test_records_left_undone_fail_even_with_nothing_to_rewrite(self) -> None:
        fake = FakeHomeserver(
            [_records(unchanged=3, not_local=3, not_learners_own=3, deactivated=2)]
        )
        code, out, _ = _main(fake)
        self.assertEqual(code, 1)
        self.assertNotIn("apply", fake.calls)
        self.assertIn("6 progress records left with old ids or a lost completion", out)

    def test_records_left_undone_at_the_limit_pass_and_are_counted(self) -> None:
        fake = FakeHomeserver([_records(unchanged=3, not_local=5)])
        code, out, _ = _main(fake)
        self.assertEqual(code, 0, out)
        self.assertIn("not_local 5", out)

    def test_deactivated_learners_are_counted_outside_the_limit(self) -> None:
        # Coordinator, 2026-11-15: their records keep old ids in rooms nobody
        # can read or write; counted on their own, never failing the night.
        fake = FakeHomeserver([_records(unchanged=3, deactivated=40)])
        code, out, summary = _main(fake)
        self.assertEqual(code, 0, out)
        self.assertIn("deactivated 40", out)
        self.assertIn(
            "40 deactivated learners' records still name old ids (outside the limit)",
            out,
        )
        self.assertIn("ok", summary.splitlines()[0])

    def test_records_left_undone_after_the_apply_fail(self) -> None:
        fake = FakeHomeserver(
            [
                _records(rewrite=1),
                _records(rewrite=1),
                _records(unchanged=1, not_local=nightly.FAILURE_LIMIT + 1),
            ]
        )
        code, _, _ = _main(fake)
        self.assertEqual(code, 1)

    def test_an_old_key_left_in_an_unchanged_record_counts(self) -> None:
        records = _records(unchanged=nightly.FAILURE_LIMIT + 1)
        for record in records:
            record["old_keys_left"] = 1
        code, out, _ = _main(FakeHomeserver([records]))
        self.assertEqual(code, 1, out)

    def test_an_unreadable_record_is_a_known_outcome(self) -> None:
        counts = nightly.progress_counts(_records(unreadable=2))
        self.assertEqual((counts["unreadable"], counts["failed"]), (2, 0))

    def test_spaces_refused_or_left_with_old_ids_fail_over_the_limit(self) -> None:
        spaces = [_space(409)] * 3 + [_space(old_ids_left=2)] * 3
        code, out, _ = _main(FakeHomeserver([_records(unchanged=1)], spaces=spaces))
        self.assertEqual(code, 1)
        self.assertIn("6 course spaces refused or left with old ids", out)

    def test_spaces_left_at_the_limit_pass_and_are_counted(self) -> None:
        spaces = [_space(409)] * 2 + [_space(old_ids_left=1)] * 3
        code, out, _ = _main(FakeHomeserver([_records(unchanged=1)], spaces=spaces))
        self.assertEqual(code, 0, out)
        self.assertIn("left_with_old_ids 3", out)


class TestEndDate(unittest.TestCase):
    def test_after_the_last_night_nothing_runs(self) -> None:
        def never(env: Any) -> Any:
            raise AssertionError("no token is minted after the last night")

        code, out, summary = _main(
            None,
            today=STAGING.last_night + datetime.timedelta(days=1),
            make_steps=never,
        )
        self.assertEqual(code, 0)
        self.assertIn("the nightly passes are over", out)
        self.assertIn("over", summary)

    def test_the_last_night_still_runs(self) -> None:
        fake = FakeHomeserver([_records(unchanged=1)])
        code, _, _ = _main(fake, today=STAGING.last_night)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, ["spaces", "dry run"])

    def test_staging_ends_on_28_november(self) -> None:
        self.assertEqual(STAGING.last_night, datetime.date(2026, 11, 28))


class TestCountsOnly(unittest.TestCase):
    def assertClean(self, text: str) -> None:
        for value in SENSITIVE:
            self.assertNotIn(value, text)

    def test_no_id_note_or_token_reaches_output_or_summary(self) -> None:
        scenarios = [
            FakeHomeserver([_records(unchanged=1, refused=1, failed=1, deactivated=1)]),
            FakeHomeserver(
                [
                    _records(rewrite=1, not_learners_own=1),
                    _records(rewrite=1, refused=1),
                    _records(rewrite=1),
                ],
                spaces=[_space(writes=True), _space(409), _space(500)],
            ),
            FakeHomeserver(
                [RuntimeError(f"{TOKEN} {ROOM} {LEARNER}")],
                spaces_error=ValueError(NOTE),
            ),
            FakeHomeserver([_records(failed=nightly.FAILURE_LIMIT + 1, rewrite=1)]),
        ]
        for fake in scenarios:
            _, out, summary = _main(fake)
            self.assertClean(out)
            self.assertClean(summary)

    def test_mint_token_never_prints_the_token_or_password(self) -> None:
        secret = json.dumps({"username": "bot", "password": PASSWORD})
        response = io.BytesIO(json.dumps({"access_token": TOKEN}).encode())
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
            nightly.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout=secret),
        ), mock.patch.object(
            nightly.urllib.request,
            "urlopen",
            return_value=contextlib.nullcontext(response),
        ) as urlopen, contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(
            stderr
        ):
            token = nightly.mint_token(STAGING)
        self.assertEqual(token, TOKEN)
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["device_id"], STAGING.device_id)
        self.assertTrue(sent["refresh_token"])
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_secret_read_failure_hides_the_cli_output(self) -> None:
        with mock.patch.object(
            nightly.subprocess,
            "run",
            return_value=mock.Mock(
                returncode=255, stdout="", stderr=f"denied {PASSWORD}"
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                nightly.read_secret(STAGING.secret_id)
        self.assertNotIn(PASSWORD, str(caught.exception))


class TestEnvironments(unittest.TestCase):
    def test_prod_data_files_are_committed_and_consistent(self) -> None:
        with open(PROD.map_path, encoding="utf-8") as f:
            can_do_map = json.load(f)
        with open(PROD.statements_path, encoding="utf-8") as f:
            statements = json.load(f)
        # The 652 old ids the production guard was loaded with on 2026-11-18.
        self.assertEqual(len(can_do_map), 652)
        merged = set(can_do_map.values())
        self.assertEqual(len(merged), 36)
        self.assertFalse(merged & set(can_do_map))
        self.assertTrue(merged <= set(statements))
        self.assertTrue(set(statements) <= set(can_do_map) | merged)

    def test_staging_and_prod_are_the_only_environments(self) -> None:
        self.assertEqual(sorted(nightly.ENVIRONMENTS), ["prod", "staging"])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                nightly.main(["--environment", "production"], make_steps=lambda e: None)

    def test_each_environment_logs_in_on_its_own_fixed_device(self) -> None:
        self.assertEqual(STAGING.device_id, "admin-script-can-do-switch-staging")
        self.assertEqual(PROD.device_id, "admin-script-can-do-switch-prod")
        self.assertEqual(PROD.homeserver, "https://matrix.pangea.chat")
        self.assertEqual(PROD.secret_id, "/prod/bot/matrix-credentials")

    def test_staging_data_files_are_committed_and_consistent(self) -> None:
        with open(STAGING.map_path, encoding="utf-8") as f:
            can_do_map = json.load(f)
        with open(STAGING.statements_path, encoding="utf-8") as f:
            statements = json.load(f)
        self.assertTrue(can_do_map)
        merged = set(can_do_map.values())
        # A merged id is never itself mapped (migrate_course_can_dos.py's docstring, Contract).
        self.assertFalse(merged & set(can_do_map))
        self.assertTrue(merged <= set(statements))


def _run(argv: List[str], today: datetime.date = BEFORE_END, **kw: Any) -> tuple:
    stdout = io.StringIO()
    with tempfile.NamedTemporaryFile("r", suffix=".out") as output:
        with tempfile.NamedTemporaryFile("r", suffix=".md") as summary:
            env = {"GITHUB_OUTPUT": output.name, "GITHUB_STEP_SUMMARY": summary.name}
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
                code = nightly.main(argv, today=today, **kw)
            return code, stdout.getvalue(), output.read(), summary.read()


@contextlib.contextmanager
def _prod_files() -> Iterator[None]:
    with tempfile.TemporaryDirectory() as data:
        os.makedirs(os.path.join(data, "prod"))
        for name in ("can-do-map.json", "can-do-statements.json"):
            with open(os.path.join(data, "prod", name), "w") as f:
                f.write("{}")
        with mock.patch.object(nightly, "DATA_DIR", data):
            yield


def _never(env: Any) -> Any:
    raise AssertionError("nothing may be contacted")


class TestDue(unittest.TestCase):
    """The workflow asks first, contacting nothing, whether tonight's pass runs."""

    def due(self, name: str, mode: str, today: datetime.date = BEFORE_END) -> str:
        code, _, output, _ = _run(
            ["--environment", name, "--due", mode], today=today, make_steps=_never
        )
        self.assertEqual(code, 0)
        return output.strip()

    def test_staging_is_scheduled_inside_its_window(self) -> None:
        self.assertEqual(self.due("staging", "scheduled"), "due=true")
        self.assertEqual(
            self.due("staging", "scheduled", STAGING.first_night), "due=true"
        )

    def test_staging_is_not_due_after_its_last_night(self) -> None:
        after = STAGING.last_night + datetime.timedelta(days=1)
        self.assertEqual(self.due("staging", "scheduled", after), "due=false")
        self.assertEqual(self.due("staging", "manual", after), "due=false")

    def test_prod_runs_nightly_from_19_november_to_2_december(self) -> None:
        self.assertEqual(PROD.first_night, datetime.date(2026, 11, 19))
        self.assertEqual(PROD.last_night, datetime.date(2026, 12, 2))
        for day, due in (
            (datetime.date(2026, 11, 18), "due=false"),
            (datetime.date(2026, 11, 19), "due=true"),
            (datetime.date(2026, 12, 2), "due=true"),
            (datetime.date(2026, 12, 3), "due=false"),
        ):
            self.assertEqual(self.due("prod", "scheduled", day), due, day)

    def test_an_environment_without_a_window_is_never_scheduled(self) -> None:
        unset = dataclasses.replace(PROD, first_night=None, last_night=None)
        with mock.patch.dict(nightly.ENVIRONMENTS, {"prod": unset}):
            self.assertEqual(self.due("prod", "scheduled"), "due=false")
            _, out, _, summary = _run(
                ["--environment", "prod", "--due", "scheduled"], make_steps=_never
            )
            self.assertEqual(self.due("prod", "manual"), "due=true")
        self.assertIn("no nightly window is set", out)
        self.assertIn("no nightly window is set", summary)

    def test_prod_by_hand_without_its_files_stops_before_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with mock.patch.object(nightly, "DATA_DIR", empty):
                code, out, output, _ = _run(
                    ["--environment", "prod", "--due", "manual"], make_steps=_never
                )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("prod has no committed map or statements yet", out)

    def test_prod_by_hand_with_its_files_is_due(self) -> None:
        with _prod_files():
            self.assertEqual(self.due("prod", "manual"), "due=true")

    def test_a_set_prod_window_schedules_it(self) -> None:
        window = dataclasses.replace(
            PROD,
            first_night=datetime.date(2026, 12, 1),
            last_night=datetime.date(2026, 12, 14),
        )
        with mock.patch.dict(nightly.ENVIRONMENTS, {"prod": window}), _prod_files():
            self.assertEqual(
                self.due("prod", "scheduled", datetime.date(2026, 11, 30)), "due=false"
            )
            self.assertEqual(
                self.due("prod", "scheduled", datetime.date(2026, 12, 1)), "due=true"
            )
            self.assertEqual(
                self.due("prod", "scheduled", datetime.date(2026, 12, 15)), "due=false"
            )


class TestProdDataFiles(unittest.TestCase):
    def test_prod_without_its_files_fails_clearly_and_contacts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with mock.patch.object(nightly, "DATA_DIR", empty):
                code, out, _, summary = _run(
                    ["--environment", "prod"], make_steps=_never
                )
        self.assertEqual(code, 1)
        for text in (out, summary):
            self.assertIn("prod has no committed map or statements yet", text)
            self.assertIn("prod/can-do-map.json", text)
            self.assertIn("Nothing was run", text)

    def test_prod_with_its_files_runs(self) -> None:
        fake = FakeHomeserver([_records(unchanged=1)])
        with _prod_files():
            code, out, _, _ = _run(
                ["--environment", "prod"], make_steps=lambda env: fake.steps()
            )
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.calls, ["spaces", "dry run"])


if __name__ == "__main__":
    unittest.main()
