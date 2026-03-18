---
applyTo: "synapse_pangea_chat/public_courses/**,synapse_pangea_chat/__init__.py,tests/test_room_preview_e2e.py,tests/staging_tests/staging_tests.py"
---

# Public Courses

Public course discovery endpoint for course catalog surfaces in clients and operator tooling.

## Route Contract

- Canonical route: `GET /_synapse/client/pangea/v1/public_courses`
- Compatibility route: `GET /_synapse/client/unstable/org.pangea/public_courses`
- Both routes must be registered to the same resource and return identical payloads.
- New callers should use the canonical `pangea/v1` route.

## Behavior

- Auth uses Matrix bearer tokens.
- Rate limiting behavior is shared across both routes.
- Query params and response schema must stay identical across aliases.
- Filtering behavior is defined by the shared `PublicCourses` resource and must not diverge by path.

## Compatibility

- Keep the unstable alias until all known callers and tests are migrated.
- When the canonical route changes, update operator tooling, bot callers, tests, and README references in the same session.

## Future Work

_(No linked issues yet.)_