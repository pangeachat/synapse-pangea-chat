"""Which test modules a CI job runs: the unit modules, or one shard of the end-to-end modules.

An end-to-end module is one that boots a real homeserver through ``BaseSynapseE2ETest``; each of
its tests starts Synapse and PostgreSQL, so those modules are most of the suite's time. They are
spread over the shards by test count, largest first, each to the shard with the fewest tests so
far, so the shards finish close together.

    python scripts/select_tests.py unit
    python scripts/select_tests.py e2e --shard 2 --of 6
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent / "tests"


def modules() -> tuple[list[tuple[str, int]], list[str]]:
    e2e, unit = [], []
    for path in sorted(TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        name = f"tests.{path.stem}"
        if "BaseSynapseE2ETest" in text:
            e2e.append(
                (name, len(re.findall(r"^\s*(?:async\s+)?def test_", text, re.M)))
            )
        else:
            unit.append(name)
    return e2e, unit


def shards(e2e: list[tuple[str, int]], count: int) -> list[list[str]]:
    out: list[list[str]] = [[] for _ in range(count)]
    load = [0] * count
    for name, tests in sorted(e2e, key=lambda m: (-m[1], m[0])):
        i = load.index(min(load))
        out[i].append(name)
        load[i] += max(tests, 1)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("unit", "e2e"))
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--of", type=int, default=1)
    args = parser.parse_args()
    e2e, unit = modules()
    if args.kind == "unit":
        print(" ".join(unit))
        return
    if not 0 <= args.shard < args.of:
        raise SystemExit(f"--shard must be between 0 and {args.of - 1}")
    print(" ".join(shards(e2e, args.of)[args.shard]))


if __name__ == "__main__":
    main()
