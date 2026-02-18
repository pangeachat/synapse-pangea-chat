Check the relevant `.github/instructions/` doc before and after coding. If it doesn't exist, create it with the user first. Follow [instructions-authoring.instructions.md](../../.github/instructions/instructions-authoring.instructions.md) for doc standards.

# synapse-pangea-chat

Unified Synapse module (Python 3.10+) bundling all Pangea Chat server-side features into `synapse_pangea_chat.PangeaChat`.

## Architecture

Single entry-point class `PangeaChat` (`synapse_pangea_chat/__init__.py`) composes seven sub-modules. Each sub-module lives in its own sub-package under `synapse_pangea_chat/`.

| Sub-module | Package | Endpoints |
|---|---|---|
| Public Courses | `public_courses/` | `GET /_synapse/client/unstable/org.pangea/public_courses` |
| Room Preview | `room_preview/` | `GET /_synapse/client/unstable/org.pangea/room_preview` |
| Room Code | `room_code/` | `POST /_synapse/client/pangea/v1/knock_with_code`, `GET /_synapse/client/pangea/v1/request_room_code` |
| Request Auto Join | `request_auto_join/` | `POST /_synapse/client/unstable/org.pangea/v1/request_auto_join` |
| Auto Accept Invite | `auto_accept_invite/` | *(third-party rules callback — no HTTP endpoint)* |
| Delete Room | `delete_room/` | `POST /_synapse/client/pangea/v1/delete_room` |
| Limit User Directory | `limit_user_directory/` | *(spam checker callback — no HTTP endpoint)* |

## Key Files

- **Entry point**: `synapse_pangea_chat/__init__.py` — `PangeaChat` class, registers all resources & callbacks
- **Config**: `synapse_pangea_chat/config.py` — `PangeaChatConfig` (attrs, all keys optional with defaults)
- **Config parsing**: `PangeaChat.parse_config()` in `__init__.py`

## Testing

### E2E tests (local Synapse + PostgreSQL)

Located in `tests/`. Each test class extends `tests.base_e2e.BaseSynapseE2ETest`, which spins up a temporary Synapse + PostgreSQL instance. Uses `aiounittest.AsyncTestCase` and `testing.postgresql`.

```sh
# Run all e2e tests
python -m unittest discover -s tests -p 'test_*.py'
```

### Staging smoke-tests (live server)

Located in `tests/staging_tests/`. Non-destructive tests against a deployed staging Synapse. Uses `unittest.IsolatedAsyncioTestCase` + `aiohttp`.

```sh
# Setup
cp tests/staging_tests/.env.example tests/staging_tests/.env
# Fill in SYNAPSE_BASE_URL and SYNAPSE_AUTH_TOKEN

# Run
python -m unittest tests.staging_tests.staging_tests
```

**Required env vars** (from `.env` file or environment):
- `SYNAPSE_BASE_URL` — e.g. `https://matrix.staging.pangea.chat`
- `SYNAPSE_AUTH_TOKEN` — valid Matrix bearer token

Missing either var → immediate `sys.exit` with `FATAL:` message.

### Conventions

- Python **≥ 3.10** (no 3.8/3.9 support)
- E2E tests: `aiounittest.AsyncTestCase`, `requests` (sync HTTP), local Synapse
- Staging tests: `unittest.IsolatedAsyncioTestCase`, `aiohttp` (async HTTP), live server
- Linting: `black`, `ruff`
- Type checking: `mypy`

### Code Style (MUST pass before committing)

Run `black --check synapse_pangea_chat tests` and `ruff check synapse_pangea_chat tests` before every commit. CI enforces these via `tox -e check_codestyle`.

Common pitfalls:
- **Empty class bodies**: Use a docstring alone (no trailing `...`). If no docstring, use `pass` on its own line. Do NOT leave an empty class body with only blank lines.
- **Stub functions**: Use two-line form `def f():\n    ...` — never one-line `def f(): ...` (black rejects it).
- **Extra blank lines**: black enforces exactly one blank line after a class docstring, two blank lines between top-level definitions. Do not add extra blank lines inside class bodies.

## Dependencies

- Runtime: `attrs`
- Dev: `matrix-synapse`, `twisted`, `aiounittest`, `aiohttp`, `psycopg2`, `testing.postgresql`, `mypy`, `black`, `ruff` (see `pyproject.toml [project.optional-dependencies] dev`)