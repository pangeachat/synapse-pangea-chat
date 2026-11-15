#!/usr/bin/env python3
"""The nightly progress pass of the merged Can-Do switch, for one environment.

Temporary: deleted with the migration endpoints (scripts/can_do_switch_data/README.md).
Run by .github/workflows/nightly-can-do-progress.yml; design in
.github/instructions/migrate-course-progress.instructions.md ("The nightly pass").

In order, against the environment's homeserver:

1. course spaces, DRY RUN only: never applied here; a space left to rewrite
   needs a person.
2. learners' progress, dry run; applied only when it finds records to rewrite
   and no more than FAILURE_LIMIT records failed or were refused.
3. after an apply, a second progress dry run, which must find nothing left.

It prints counts only. The reports name rooms, courses and learners and quote
teachers' notes, and this repository's Actions logs are public, so no report is
written to disk, no room, course or learner id is printed, and an error is
printed as its type, never its message. The admin token is minted in memory from
the bot's Matrix credentials in AWS Secrets Manager and never printed.

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
# More failed or refused progress records than this, in any run, needs a person.
FAILURE_LIMIT = 5
PROGRESS_OUTCOMES = (
    "rewrite",
    "unchanged",
    "deactivated",
    "refused",
    "not_learners_own",
    "not_local",
    "failed",
)


@dataclass(frozen=True)
class Environment:
    name: str
    homeserver: str
    secret_id: str
    device_id: str
    last_night: datetime.date

    @property
    def map_path(self) -> str:
        return os.path.join(DATA_DIR, self.name, "can-do-map.json")

    @property
    def statements_path(self) -> str:
        return os.path.join(DATA_DIR, self.name, "can-do-statements.json")


# Staging switched on 2026-11-14 (pangeachat/.github#832); two weeks of nightly passes.
ENVIRONMENTS = {
    "staging": Environment(
        name="staging",
        homeserver="https://matrix.staging.pangea.chat",
        secret_id="/staging/bot/matrix-credentials",
        # The device the laptop pass already uses, so no new device fans out
        # (org access-controls.instructions.md, "Matrix service-account sessions").
        device_id="admin-script-can-do-switch-staging",
        last_night=datetime.date(2026, 11, 28),
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
    result["keys_moved"] = sum(r.get("keys_moved", 0) for r in records)
    result["completions_kept"] = sum(r.get("completions_kept", 0) for r in records)
    return result


def space_counts(report: Dict[str, Any]) -> Dict[str, int]:
    spaces = report["spaces"]
    return {
        "checked": len(spaces),
        "to_rewrite": sum(map(migration.to_write, spaces)),
        "refused_no_local_instructor": sum(s["status"] == 409 for s in spaces),
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
    except Exception as e:  # noqa: BLE001 - recorded as a problem; message withheld
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

    try:
        first = progress_counts(steps.progress(True))
    except Exception as e:  # noqa: BLE001 - recorded as a problem; message withheld
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
        return out

    try:
        applied = progress_counts(steps.progress(False))
    except Exception as e:  # noqa: BLE001 - recorded as a problem; message withheld
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
    except Exception as e:  # noqa: BLE001 - recorded as a problem; message withheld
        out.problems.append(f"second progress dry run failed ({type(e).__name__})")
        return out
    out.say(f"second progress dry run: {format_counts(second)}")
    if second["rewrite"]:
        out.problems.append(
            f"second dry run still finds {second['rewrite']} records to rewrite"
        )
    return out


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
    args = parser.parse_args(argv)
    env = ENVIRONMENTS[args.environment]
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")

    if today > env.last_night:
        out = Outcome(lines=[f"past {env.last_night}: the nightly passes are over"])
        print(out.lines[0])
        write_summary(env, out, summary)
        return 0

    try:
        steps = make_steps(env)
    except (
        Exception
    ) as e:  # noqa: BLE001 - reported and fails the run; message withheld
        out = Outcome(problems=[f"minting the admin token failed ({type(e).__name__})"])
    else:
        out = run_night(steps)

    for line in out.lines:
        print(line)
    for problem in out.problems:
        print(f"::error::{problem}")
    write_summary(env, out, summary)
    if out.problems:
        print("NEEDS A PERSON")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
