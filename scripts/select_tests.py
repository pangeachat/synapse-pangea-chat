"""Which test classes a CI job runs: the unit classes, or one shard of the end-to-end classes.

An end-to-end class is one that boots a real homeserver through ``BaseSynapseE2ETest``. Its tests
share that homeserver, so a class costs about one boot plus a little per test, except the few whose
tests wait on purpose (background processors, held pushes, rate-limit windows), whose measured time
is listed below. The shards are balanced on that cost, largest class first, each to the shard with
the least work so far.

The classes come from the same discovery a local ``python -m unittest discover -s tests -t .``
runs, so between the unit job and the shards every test runs exactly once.

    python scripts/select_tests.py unit
    python scripts/select_tests.py e2e --shard 2 --of 3
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent

# Seconds on a CI runner, from the per-test durations a CI run prints: a class
# with one quick test takes about BOOT_SECONDS, and each further test adds about
# TEST_SECONDS.
BOOT_SECONDS = 2.3
TEST_SECONDS = 0.4

# Classes that run well over that estimate because their tests sleep or wait,
# with their measured CI seconds. A class missing from here is estimated; a
# stale entry only costs balance. Refresh from the "Slowest test durations"
# section of the E2E jobs' logs (a class's time is the sum of its tests').
MEASURED_SECONDS = {
    "tests.test_activity_session_previews_e2e.TestActivitySessionPreviewsE2E": 35,
    "tests.test_delayed_push_e2e.TestDelayedPushRingE2E": 19,
    "tests.test_room_code_e2e.TestE2E": 16,
    "tests.test_logcontext_e2e.TestOutboundCallLogcontext": 13,
    "tests.test_delete_user_e2e.TestDeleteUserProcessor": 12,
    "tests.test_admin_code_e2e.TestAdminCodeE2E": 11,
    "tests.test_user_activity_e2e.TestUserActivityE2E": 11,
    "tests.test_export_user_data_e2e.TestExportUserDataProcessor": 10,
    "tests.test_create_course_space_e2e.TestCreateCourseSpaceE2E": 8,
    "tests.test_logcontext_e2e.TestModerationLogcontext": 6,
}


def _cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        else:
            yield item


def classes() -> tuple[dict[str, int], list[str]]:
    """({e2e class id: test count}, [unit class ids]), in discovery order."""
    sys.path.insert(0, str(ROOT))
    from tests.base_e2e import BaseSynapseE2ETest

    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    if loader.errors:
        raise SystemExit("Test discovery failed:\n" + "\n".join(loader.errors))
    e2e: dict[str, int] = {}
    unit: dict[str, None] = {}
    for case in _cases(suite):
        cls = type(case)
        name = f"{cls.__module__}.{cls.__qualname__}"
        if issubclass(cls, BaseSynapseE2ETest):
            e2e[name] = e2e.get(name, 0) + 1
        else:
            unit[name] = None
    return e2e, list(unit)


def shards(e2e: dict[str, int], count: int) -> list[list[str]]:
    out: list[list[str]] = [[] for _ in range(count)]
    load = [0.0] * count
    for name in MEASURED_SECONDS.keys() - e2e.keys():
        print(f"select_tests: {name} is no longer an E2E class", file=sys.stderr)
    cost = {
        name: MEASURED_SECONDS.get(name, BOOT_SECONDS + TEST_SECONDS * tests)
        for name, tests in e2e.items()
    }
    for name in sorted(e2e, key=lambda n: (-cost[n], n)):
        i = load.index(min(load))
        out[i].append(name)
        load[i] += cost[name]
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("unit", "e2e"))
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--of", type=int, default=1)
    args = parser.parse_args()
    e2e, unit = classes()
    if args.kind == "unit":
        print(" ".join(unit))
        return
    if not 0 <= args.shard < args.of:
        raise SystemExit(f"--shard must be between 0 and {args.of - 1}")
    print(" ".join(sorted(shards(e2e, args.of)[args.shard])))


if __name__ == "__main__":
    main()
