"""Unit tests for the nightly Can-Do progress pass's decisions
(scripts/nightly_can_do_progress.py; migrate-course-progress.instructions.md,
"The nightly pass")."""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional
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


def _space(status: int = 200, writes: bool = False) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "writes": [{"room_id": COURSE}] if writes else [],
        "course_missions": {"notes_trimmed": [{"dropped": NOTE}]},
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
    def test_only_staging_is_accepted(self) -> None:
        self.assertEqual(sorted(nightly.ENVIRONMENTS), ["staging"])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                nightly.main(["--environment", "production"], make_steps=lambda e: None)

    def test_staging_data_files_are_committed_and_consistent(self) -> None:
        with open(STAGING.map_path, encoding="utf-8") as f:
            can_do_map = json.load(f)
        with open(STAGING.statements_path, encoding="utf-8") as f:
            statements = json.load(f)
        self.assertTrue(can_do_map)
        merged = set(can_do_map.values())
        # A merged id is never itself mapped (migrate-course-can-dos, Contract).
        self.assertFalse(merged & set(can_do_map))
        self.assertTrue(merged <= set(statements))


if __name__ == "__main__":
    unittest.main()
