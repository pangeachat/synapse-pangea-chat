---
description: "Troubleshooting local synapse-pangea-chat unittest discovery and E2E suite failures."
applyTo: "tests/**,synapse_pangea_chat/assign_room_membership/**,synapse_pangea_chat/user_activity/**,.github/instructions/testing.instructions.md"
---

# Synapse Module Local Test Suite

## Symptoms

- `python -m unittest discover -s tests -p 'test_*.py'` imports E2E modules as top-level modules and fails relative imports like `from .base_e2e import BaseSynapseE2ETest`.
- Assign-room-membership E2E tests fail with `'RoomVersion' object has no attribute 'msc4289_creator_power_enabled'` on Synapse versions before that room-version flag exists.
- User-activity E2E tests can fail when they assert mixed-case Matrix IDs from registration input or rely on `user_ips` rows appearing after login.

## Root Cause

- `unittest discover` needs the repository root as top-level (`-t .`) so `tests` remains a package.
- Synapse room-version capability flags vary by Synapse release; module code must treat missing future flags as disabled.
- Synapse lowercases registered localparts from CLI registration, and local test Synapse may not flush `user_ips` during short-lived E2E tests.

## Fix

- Run local integration tests with `python -m unittest discover -s tests -t . -p 'test_*.py'`.
- Use `getattr(room_version, "msc4289_creator_power_enabled", False)` for optional room-version features.
- In E2E tests, use user IDs returned by login/registration APIs instead of typed registration names when asserting endpoint output.
- Prefer deterministic message events over `user_ips` login rows when testing inactivity filters.

## Verification

- `ruff check synapse_pangea_chat tests` passes.
- Targeted E2E regressions pass before rerunning the suite.
- Full local suite passes with `PATH="/usr/lib/postgresql/16/bin:$PATH" LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/python -m unittest discover -s tests -t . -p 'test_*.py'`.
