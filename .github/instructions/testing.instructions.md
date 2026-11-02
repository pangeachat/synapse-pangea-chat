---
applyTo: "**/*test*,**/tests/**"
---

# Testing Guide (synapse-pangea-chat)

Follows the [cross-repo testing strategy](../../../.github/.github/instructions/testing.instructions.md) — see that doc for the bucket framework (unit / integration / smoke / eval / load), conventions, and rationale. This doc covers synapse module-specific details only.

## Stack

- **Framework**: `unittest` (Python standard library)
- **Language**: Python 3.10+
- **Async tests**: `aiounittest.AsyncTestCase` (integration), `unittest.IsolatedAsyncioTestCase` (staging)

## Test Organization

This repo uses a different model than the other Python repos — no `.txt` registry files. Tests are organized by what they hit:

- `tests/` — Integration tests that spin up a local Synapse + PostgreSQL instance via `testing.postgresql`. Each test class extends `tests.base_e2e.BaseSynapseE2ETest`. These are integration-tier (local internal infrastructure, no paid APIs) despite the `e2e` naming in the base class.
- `tests/staging_tests/` — Staging smoke-tests against a live deployed Synapse. Non-destructive, uses `aiohttp`

## Writing an E2E test

The tests of one class share one homeserver, because booting Synapse is most of what an E2E test costs. The class, not the test, is the unit of isolation:

- Get the server with `await self.homeserver(...)`. Every test in a class asks for the same configuration. A test that needs a different one belongs in its own class; otherwise the server restarts at every change.
- Register users with `new_user(name)`, which gives each test its own localpart. Suffix `self.tag` onto anything else that must be unique on the server: email addresses, aliases, codes.
- Assert only on what the test created. Earlier tests' users, rooms, rows, log lines and in-process state (caches, per-address rate limiters, background processors) are still on the server. Scope listings and queries to this test's IDs, and read logs through `server_log()`.
- Start anything the configuration points at (an SMTP sink, a mock CMS, a Sygnal stub) in `setUpClass`, so the configuration stays the same for the whole class.
- `start_test_synapse` boots a server for one test alone; it is only for tests of the boot and teardown cycle.

Each server listens on a free port, so the suite runs alongside a local-stack Synapse.

## Commands

```bash
# Integration tests (local Synapse + PostgreSQL)
python -m unittest discover -s tests -t . -p 'test_*.py'

# Staging smoke-tests (requires .env with SYNAPSE_BASE_URL and SYNAPSE_AUTH_TOKEN)
python -m unittest tests.staging_tests.staging_tests
```

## Local Setup (macOS arm64)

Integration tests need PostgreSQL, OpenSSL, libpq, and a Python 3.10 or later. Concrete recipe:

```bash
# One-time toolchain installs
brew install postgresql@17 libpq openssl@3 python@3.14

# Per-checkout: create venv and install dev deps
python3.14 -m venv .venv
PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:$PATH" \
LDFLAGS="-L/opt/homebrew/opt/openssl@3/lib -L/opt/homebrew/opt/libpq/lib" \
CPPFLAGS="-I/opt/homebrew/opt/openssl@3/include -I/opt/homebrew/opt/libpq/include" \
.venv/bin/pip install -e ".[dev]"

# Run tests (postgres@17 + UTF-8 locale required at run time, not just install time)
PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:$PATH" \
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
PYTHONPATH="$PWD" \
.venv/bin/python -m unittest discover -s tests -t . -p 'test_*.py'
```

Why each piece:

- **`postgresql@17` before `libpq` on PATH** — both ship `initdb`, but `testing.postgresql` needs the one with `postgres` (the server binary) next to it; libpq's `initdb` is client-only. With neither on PATH, every E2E class errors at setup with `RuntimeError: command not found: initdb`.
- **`LC_ALL=en_US.UTF-8` at run time** — Postgres 17 on macOS exits with `postmaster became multithreaded during startup` if `LC_ALL` is unset, but `LC_ALL=C` produces `SQL_ASCII` databases that synapse rejects with `IncorrectDatabaseSetup`. UTF-8 is the only locale that satisfies both.
- **Any Python 3.10 or later** — the pinned matrix-synapse ships a prebuilt `abi3` wheel for arm64 macOS, so pip installs it without compiling anything and no Rust toolchain is needed. Use whichever current Python Homebrew ships; CI runs 3.13.
- **The Synapse version comes from the `pyproject.toml` pin, not from this doc.** This recipe used to carry workarounds tied to one Synapse release (a Rust build, `setuptools<81`, "3.13 not 3.14"), and they stopped being true when the pin moved on. When the pin changes, check the recipe still installs cleanly rather than trusting notes about an older release.
- **`PYTHONPATH="$PWD"` when the venv was installed from another checkout** (a worktree using the main checkout's `.venv`) — the test Synapse runs in a temporary directory, so without it Synapse imports this module from wherever `pip install -e` was run and the tests report on that copy, not the worktree's. It is harmless in the checkout the venv was installed from.
- **Don't run these tests from the repo's `.tox/py` environment.** It can hold an older matrix-synapse than the pin and an old, non-editable copy of this module, and the test Synapse loads that copy instead of your working tree. Tests run there report on stale code: `create_course_space` returned 500 on unmodified `main` in such an environment. Use the venv above.

## CI

CI runs on every push to main and every PR: code style (`tox -e check_codestyle`), types (`tox -e check_types`), the unit tests in one job, and the end-to-end classes (those that boot a homeserver through `BaseSynapseE2ETest`) spread over parallel jobs against the runner's preinstalled PostgreSQL. `scripts/select_tests.py` takes the classes from the same discovery a local `unittest discover` runs and decides which each job runs, so between them every test runs exactly once. It balances the shards on each class's cost: one boot plus a little per test, or a measured time for the few classes that wait on purpose. The `Unit & integration tests` check passes only when the unit job and every shard passed. Staging smoke-tests still run only locally/manually.

## Manual Testing

- Deploy to staging via Ansible, then run staging smoke-tests
- SSH to staging and check Synapse logs: `sudo journalctl -fu matrix-synapse.service`

## Code Style (MUST pass before committing)

Run `black --check synapse_pangea_chat tests` and `ruff check synapse_pangea_chat tests` before every commit. CI enforces these via `tox -e check_codestyle`.

Common pitfalls:
- **Empty class bodies**: Use a docstring alone (no trailing `...`). If no docstring, use `pass` on its own line. Do NOT leave an empty class body with only blank lines.
- **Stub functions**: Use two-line form `def f():\n    ...` — never one-line `def f(): ...` (black rejects it).
- **Extra blank lines**: black enforces exactly one blank line after a class docstring, two blank lines between top-level definitions. Do not add extra blank lines inside class bodies.

## Future Work

_(No linked issues yet.)_
