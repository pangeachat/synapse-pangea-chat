---
applyTo: "**/*test*,**/tests/**"
---

# Testing Guide (synapse-pangea-chat)

Follows the [cross-repo testing strategy](../../../.github/instructions/testing.instructions.md) — see that doc for tier definitions (unit / integration / e2e), conventions, and rationale. This doc covers synapse module-specific details only.

## Stack

- **Framework**: `unittest` (Python standard library)
- **Language**: Python 3.10+
- **Async tests**: `aiounittest.AsyncTestCase` (integration), `unittest.IsolatedAsyncioTestCase` (staging)

## Test Organization

This repo uses a different model than the other Python repos — no `.txt` registry files. Tests are organized by what they hit:

- `tests/` — Integration tests that spin up a local Synapse + PostgreSQL instance via `testing.postgresql`. Each test class extends `tests.base_e2e.BaseSynapseE2ETest`. These are integration-tier (local internal infrastructure, no paid APIs) despite the `e2e` naming in the base class.
- `tests/staging_tests/` — Staging smoke-tests against a live deployed Synapse. Non-destructive, uses `aiohttp`

## Commands

```bash
# Integration tests (local Synapse + PostgreSQL)
python -m unittest discover -s tests -p 'test_*.py'

# Staging smoke-tests (requires .env with SYNAPSE_BASE_URL and SYNAPSE_AUTH_TOKEN)
python -m unittest tests.staging_tests.staging_tests
```

## Local Setup (macOS arm64)

Integration tests need PostgreSQL, OpenSSL, libpq, Rust (matrix-synapse 1.124.0 has no arm64 macOS wheel — pip builds it from sdist), and a Python with prebuilt synapse-extension wheels. Concrete recipe:

```bash
# One-time toolchain installs
brew install postgresql@17 libpq openssl@3 rust python@3.13

# Per-checkout: create venv with python@3.13 and install dev deps
python3.13 -m venv .venv
PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:$PATH" \
LDFLAGS="-L/opt/homebrew/opt/openssl@3/lib -L/opt/homebrew/opt/libpq/lib" \
CPPFLAGS="-I/opt/homebrew/opt/openssl@3/include -I/opt/homebrew/opt/libpq/include" \
.venv/bin/pip install -e ".[dev]"
.venv/bin/pip install "setuptools<81"   # synapse 1.124.0 still imports pkg_resources

# Run tests (postgres@17 + UTF-8 locale required at run time, not just install time)
PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:$PATH" \
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Why each piece:

- **`postgresql@17` before `libpq` on PATH** — both ship `initdb`, but `testing.postgresql` needs the one with `postgres` (the server binary) next to it; libpq's `initdb` is client-only.
- **`LC_ALL=en_US.UTF-8` at run time** — Postgres 17 on macOS exits with `postmaster became multithreaded during startup` if `LC_ALL` is unset, but `LC_ALL=C` produces `SQL_ASCII` databases that synapse rejects with `IncorrectDatabaseSetup`. UTF-8 is the only locale that satisfies both.
- **`setuptools<81`** — synapse 1.124.0 imports `pkg_resources`, which setuptools 81+ no longer ships by default.
- **Rust** — matrix-synapse's PyPI release for 1.124.0 has no arm64 macOS wheel, so pip falls back to the sdist, which compiles the `synapse_rust` crate via `setuptools-rust`.
- **Python 3.13, not 3.14** — synapse-extension wheels for 1.124.0 don't cover cp314 yet; using `python@3.13` lets the install succeed without rebuilding everything from source.

## CI

Code style is enforced via `tox -e check_codestyle` (black + ruff). Tests are run locally — no GitHub Actions test workflow.

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
