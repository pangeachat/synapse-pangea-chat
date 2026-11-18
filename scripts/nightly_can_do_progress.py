#!/usr/bin/env python3
"""The nightly progress pass of the merged Can-Do switch, for one environment.

Temporary: deleted with the migration endpoints (scripts/can_do_switch_data/README.md).
Run by .github/workflows/nightly-can-do-progress.yml, nightly for two weeks
after an environment's switch and on demand (Will, 2026-11-15); each
environment's window is in ENVIRONMENTS below. With the stale progress guard
live, a rewrite found means a stale write got past it. The migration rules are
in synapse_pangea_chat/migrate_course_can_dos/.

In order, against the environment's homeserver:

1. course spaces, DRY RUN only: never applied here; a space left to rewrite
   needs a person, and so do more than FAILURE_LIMIT spaces refused or left
   with old ids.
2. learners' progress, dry run; applied only when it finds records to rewrite
   and no more than FAILURE_LIMIT records failed or were refused.
3. after an apply, a second progress dry run, which must find nothing left.

Whether or not anything was applied, more than FAILURE_LIMIT records left with
old ids or a lost completion (refused, on another server, not the learner's own,
failed, or an old key left in place) in the last dry run needs a person.
Deactivated learners' records are counted on their own, outside that limit:
their old ids stay in rooms no app or account can read or write.

It prints counts only. The reports name rooms, courses and learners and quote
teachers' notes, and this repository's Actions logs are public, so no report is
written to disk, no room, course or learner id is printed, and an error is
printed as its type, never its message. The admin token is minted in memory from
that environment's bot Matrix credentials in AWS Secrets Manager, through a
role limited to that one secret and this repository's main branch, on a fixed
device per environment that is never logged out; it is never printed.

The operator script's progress apply (scripts/migrate_course_can_dos.py) exits
non-zero on the same undone records, listing deactivated ones apart.

Exit codes: 0 done or nothing to do, 1 something needs a person.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import migrate_course_can_dos as migration  # noqa: E402

DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "can_do_switch_data"
)
# More than this many failed or refused progress records in a run, records left
# undone, or course spaces refused or left with old ids, needs a person.
FAILURE_LIMIT = 5
# Outcomes that leave a record holding old ids or missing a completion. A
# deactivated learner's record is reported on its own, outside the limit.
UNRESOLVED_OUTCOMES = (
    "refused",
    "not_learners_own",
    "not_local",
    "failed",
)
PROGRESS_OUTCOMES = (
    "rewrite",
    "unchanged",
    "unreadable",
    "deactivated",
    *UNRESOLVED_OUTCOMES,
)


@dataclass(frozen=True)
class Environment:
    name: str
    homeserver: str
    secret_id: str
    device_id: str
    # The scheduled window, both inclusive. With no first night the schedule
    # never runs this environment; a person starts it by hand.
    first_night: Optional[datetime.date]
    last_night: Optional[datetime.date]

    def over(self, today: datetime.date) -> bool:
        return self.last_night is not None and today > self.last_night

    def scheduled(self, today: datetime.date) -> bool:
        return (
            self.first_night is not None
            and self.first_night <= today
            and not self.over(today)
        )

    @property
    def map_path(self) -> str:
        return os.path.join(DATA_DIR, self.name, "can-do-map.json")

    @property
    def statements_path(self) -> str:
        return os.path.join(DATA_DIR, self.name, "can-do-statements.json")


# Each environment's login device is fixed, so the homeserver reuses it and no
# login fans out a device-list change; it is never logged out or deleted (org
# access-controls.instructions.md, "Matrix service-account sessions").
ENVIRONMENTS = {
    "staging": Environment(
        name="staging",
        homeserver="https://matrix.staging.pangea.chat",
        secret_id="/staging/bot/matrix-credentials",
        # The device the laptop pass already used.
        device_id="admin-script-can-do-switch-staging",
        # Staging switched on 2026-11-14 (pangeachat/.github#832); two weeks.
        first_night=datetime.date(2026, 11, 15),
        last_night=datetime.date(2026, 11, 28),
    ),
    "prod": Environment(
        name="prod",
        homeserver="https://matrix.pangea.chat",
        secret_id="/prod/bot/matrix-credentials",
        device_id="admin-script-can-do-switch-prod",
        # Production switched on 2026-11-18 (step 16 of #832's runbook); two weeks.
        first_night=datetime.date(2026, 11, 19),
        last_night=datetime.date(2026, 12, 2),
    ),
}


@dataclass
class Outcome:
    """What one night did, as counts only."""

    lines: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.lines.append(line)


def progress_counts(records: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter(r["outcome"] for r in records)
    unknown = sorted(set(counts) - set(PROGRESS_OUTCOMES))
    if unknown:
        # A new outcome is not a known-safe one: count it as failed.
        counts["failed"] += sum(counts.pop(o) for o in unknown)
    result = {o: counts.get(o, 0) for o in PROGRESS_OUTCOMES}
    result["records"] = len(records)
    result["courses"] = len({r["course_id"] for r in records})
    for key in ("keys_moved", "completions_kept", "completions_restored"):
        result[key] = sum(r.get(key, 0) for r in records)
    result["old_keys_left"] = sum(1 for r in records if r.get("old_keys_left"))
    result["unresolved"] = sum(
        1
        for r in records
        if r["outcome"] in UNRESOLVED_OUTCOMES
        or (r["outcome"] != "deactivated" and r.get("old_keys_left"))
    )
    return result


def _left_with_old_ids(space: Dict[str, Any]) -> bool:
    body = space["body"] if space["status"] == 200 else {}
    return bool(
        (body.get("course_missions") or {}).get("old_ids_left")
        or (body.get("teacher_mode") or {}).get("outcome") == "unreadable"
    )


def space_counts(report: Dict[str, Any]) -> Dict[str, int]:
    spaces = report["spaces"]
    return {
        "checked": len(spaces),
        "to_rewrite": sum(map(migration.to_write, spaces)),
        "refused_no_local_instructor": sum(s["status"] == 409 for s in spaces),
        "left_with_old_ids": sum(map(_left_with_old_ids, spaces)),
        "failed": sum(s["status"] not in (200, 409) for s in spaces),
    }


def bad_records(counts: Dict[str, int]) -> int:
    return counts["failed"] + counts["refused"]


def format_counts(counts: Dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items())


@dataclass
class Steps:
    """The calls a night makes, injectable so the decisions can be tested."""

    space_dry_run: Callable[[], Dict[str, Any]]
    progress: Callable[[bool], List[Dict[str, Any]]]


def run_night(steps: Steps) -> Outcome:
    out = Outcome()

    try:
        spaces = space_counts(steps.space_dry_run())
    # silent-ok: not silent - recorded as a problem that fails the run; the message is withheld because the Actions logs are public
    except Exception as e:
        out.problems.append(f"course-space dry run failed ({type(e).__name__})")
    else:
        out.say(f"course spaces (dry run only): {format_counts(spaces)}")
        if spaces["failed"]:
            out.problems.append(f"{spaces['failed']} course spaces failed the dry run")
        if spaces["to_rewrite"]:
            out.problems.append(
                f"{spaces['to_rewrite']} course spaces still name old Can-Dos; "
                "the space apply is run by a person"
            )
        left = spaces["refused_no_local_instructor"] + spaces["left_with_old_ids"]
        if left > FAILURE_LIMIT:
            out.problems.append(
                f"{left} course spaces refused or left with old ids "
                f"(limit {FAILURE_LIMIT})"
            )

    try:
        first = progress_counts(steps.progress(True))
    # silent-ok: not silent - recorded as a problem that fails the run; the message is withheld because the Actions logs are public
    except Exception as e:
        out.problems.append(f"progress dry run failed ({type(e).__name__})")
        return out
    out.say(f"progress dry run: {format_counts(first)}")

    if bad_records(first) > FAILURE_LIMIT:
        out.problems.append(
            f"{bad_records(first)} failed or refused records in the dry run "
            f"(limit {FAILURE_LIMIT}); not applied"
        )
        return out
    if not first["rewrite"]:
        out.say("nothing to rewrite; not applied")
        _check_unresolved(out, first)
        return out

    try:
        applied = progress_counts(steps.progress(False))
    # silent-ok: not silent - recorded as a problem that fails the run; the message is withheld because the Actions logs are public
    except Exception as e:
        out.problems.append(f"progress apply failed ({type(e).__name__})")
    else:
        out.say(f"progress apply: {format_counts(applied)}")
        if bad_records(applied) > FAILURE_LIMIT:
            out.problems.append(
                f"{bad_records(applied)} failed or refused records in the apply "
                f"(limit {FAILURE_LIMIT})"
            )

    try:
        second = progress_counts(steps.progress(True))
    # silent-ok: not silent - recorded as a problem that fails the run; the message is withheld because the Actions logs are public
    except Exception as e:
        out.problems.append(f"second progress dry run failed ({type(e).__name__})")
        return out
    out.say(f"second progress dry run: {format_counts(second)}")
    if second["rewrite"]:
        out.problems.append(
            f"second dry run still finds {second['rewrite']} records to rewrite"
        )
    _check_unresolved(out, second)
    return out


def _check_unresolved(out: Outcome, counts: Dict[str, int]) -> None:
    if counts["deactivated"]:
        out.say(
            f"{counts['deactivated']} deactivated learners' records still name old "
            "ids (outside the limit)"
        )
    if counts["unresolved"] > FAILURE_LIMIT:
        out.problems.append(
            f"{counts['unresolved']} progress records left with old ids or a lost "
            f"completion (limit {FAILURE_LIMIT})"
        )


def read_secret(secret_id: str) -> Dict[str, str]:
    """The secret's JSON, read with the AWS CLI's ambient credentials."""
    proc = subprocess.run(
        [
            "aws",
            "secretsmanager",
            "get-secret-value",
            "--region",
            "us-east-1",
            "--secret-id",
            secret_id,
            "--query",
            "SecretString",
            "--output",
            "text",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"reading the secret failed (aws exit {proc.returncode})")
    return json.loads(proc.stdout)


def mint_token(env: Environment) -> str:
    cred = read_secret(env.secret_id)
    body = json.dumps(
        {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": cred["username"]},
            "password": cred["password"],
            "device_id": env.device_id,
            "refresh_token": True,
        }
    ).encode()
    request = urllib.request.Request(
        env.homeserver + "/_matrix/client/v3/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def _load(path: str) -> Dict[str, str]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def live_steps(env: Environment, token: str) -> Steps:
    can_do_map = _load(env.map_path)
    statements = _load(env.statements_path)
    return Steps(
        space_dry_run=lambda: migration.dry_run(
            env.homeserver, token, can_do_map, None, statements
        ),
        progress=lambda dry_run: migration.run_progress(
            env.homeserver, token, can_do_map, dry_run
        ),
    )


def write_summary(env: Environment, out: Outcome, path: Optional[str]) -> None:
    if not path:
        return
    status = "needs a person" if out.problems else "ok"
    lines = [f"## Can-Do progress pass, {env.name}: {status}", ""]
    lines += [f"- {line}" for line in out.lines]
    lines += [f"- **problem:** {p}" for p in out.problems]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(
    argv: Optional[List[str]] = None,
    today: Optional[datetime.date] = None,
    make_steps: Callable[[Environment], Steps] = lambda env: live_steps(
        env, mint_token(env)
    ),
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--environment", required=True, choices=sorted(ENVIRONMENTS))
    parser.add_argument(
        "--due",
        choices=("scheduled", "manual"),
        help="Only say whether the pass runs tonight (due=true|false, also to "
        "GITHUB_OUTPUT); contacts nothing.",
    )
    args = parser.parse_args(argv)
    env = ENVIRONMENTS[args.environment]
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")

    if args.due:
        due = not env.over(today) and (args.due == "manual" or env.scheduled(today))
        missing = _missing_data(env) if due else None
        if missing:
            # Stops the job before any credentials are assumed.
            _report(env, missing, summary)
            return 1
        _write_output("due", "true" if due else "false")
        print(f"due={str(due).lower()}")
        if not due:
            reason = (
                f"past {env.last_night}: the nightly passes are over"
                if env.over(today)
                else "not scheduled: no nightly window is set for this environment"
            )
            print(reason)
            write_summary(env, Outcome(lines=[reason]), summary)
        return 0

    if env.over(today):
        out = Outcome(lines=[f"past {env.last_night}: the nightly passes are over"])
        print(out.lines[0])
        write_summary(env, out, summary)
        return 0

    missing = _missing_data(env)
    if missing:
        # Checked before any token is minted, so a run without them contacts nothing.
        _report(env, missing, summary)
        return 1

    try:
        steps = make_steps(env)
    # silent-ok: not silent - recorded as a problem that fails the run; the message is withheld because the Actions logs are public
    except Exception as e:
        out = Outcome(problems=[f"minting the admin token failed ({type(e).__name__})"])
    else:
        out = run_night(steps)

    _report(env, out, summary)
    return 1 if out.problems else 0


def _missing_data(env: Environment) -> Optional[Outcome]:
    missing = [
        os.path.relpath(path, os.path.dirname(DATA_DIR))
        for path in (env.map_path, env.statements_path)
        if not os.path.exists(path)
    ]
    if not missing:
        return None
    return Outcome(
        problems=[
            f"{env.name} has no committed map or statements yet ("
            + ", ".join(missing)
            + "); they are written during the switch. Nothing was run."
        ]
    )


def _report(env: Environment, out: Outcome, summary: Optional[str]) -> None:
    for line in out.lines:
        print(line)
    for problem in out.problems:
        print(f"::error::{problem}")
    write_summary(env, out, summary)
    if out.problems:
        print("NEEDS A PERSON")


def _write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


if __name__ == "__main__":
    sys.exit(main())
