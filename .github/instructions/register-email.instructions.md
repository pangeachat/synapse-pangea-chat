---
applyTo: "synapse_pangea_chat/register_email/**,synapse_pangea_chat/__init__.py,tests/test_register_email_e2e.py"
---

# Register Email Request Token

Pangea exposes a registration-email endpoint that validates the requested username before Synapse sends a verification email.

## Route Contract

- Canonical route: `POST /_synapse/client/pangea/v1/register/email/requestToken`
- The route stays unauthenticated because it is part of account creation.
- The request body must include `username`, `client_secret`, `email`, and `send_attempt`.
- `next_link` is optional and should retain Synapse-compatible behavior.

## Behavior

- Username validation happens before any email-side effect so we never send a verification email for a taken or invalid username.
- Missing or invalid request parameters must preserve Matrix-compatible error semantics.
- IP-based rate limiting is part of the endpoint contract because the route is unauthenticated.
- The endpoint should delegate to Synapse's existing threepid validation flow once Pangea-specific username checks pass.

## Email Configuration

- If Synapse email verification is disabled, the route must remain registered and return a clear error instead of disappearing.
- When email verification is enabled, this endpoint must use Synapse's registration mailer/templates rather than a separate mail pipeline.
- Existing Synapse protections around threepid allowlists, existing email ownership, and idempotent resend behavior must remain intact.

## Compatibility

- Keep the endpoint behavior aligned with Synapse's built-in `register/email/requestToken` flow wherever Pangea is not intentionally adding policy.
- Type-only or test-only fixes should prefer local narrowing and annotations over behavior changes.

## Key Files

- Endpoint registration: [synapse_pangea_chat/__init__.py](../../synapse_pangea_chat/__init__.py)
- Endpoint implementation: [synapse_pangea_chat/register_email/register_email.py](../../synapse_pangea_chat/register_email/register_email.py)
- End-to-end tests: [tests/test_register_email_e2e.py](../../tests/test_register_email_e2e.py)

## Future Work

_Last updated: 2026-03-20_

_(No linked issues or discussions yet.)_