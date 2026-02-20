---
applyTo: "**/*test*,**/tests/**"
---

# Testing Guide (synapse-pangea-chat)

Follows the [cross-repo testing strategy](../../.github/instructions/testing.instructions.md) — see that doc for tier definitions (unit / integration / e2e), conventions, and rationale. This doc covers synapse module-specific details only.

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

## CI

Code style is enforced via `tox -e check_codestyle` (black + ruff). Tests are run locally — no GitHub Actions test workflow.

## Manual Testing

- Deploy to staging via Ansible, then run staging smoke-tests
- SSH to staging and check Synapse logs: `sudo journalctl -fu matrix-synapse.service`

## Future Work

_(No linked issues yet.)_
