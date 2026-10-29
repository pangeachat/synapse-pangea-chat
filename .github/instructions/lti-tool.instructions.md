---
applyTo: "synapse_pangea_chat/lti/**"
description: "The Synapse module's LTI 1.3 tool for Canvas: registration and operator approval, launch validation, the launch hand-offs, course connect and the roster import."
---

# LTI tool (Canvas)

The module is Pangea's LTI 1.3 tool. [lms-integration](../../../.github/.github/instructions/lms-integration.instructions.md) owns the install, connect, import and launch flows and the "never" rules; [seats](../../../.github/.github/instructions/seats.instructions.md) owns the invitations an import creates. This doc owns what the module decides in carrying them out.

Canvas is not released: the tool's routes are registered only when the module config carries an `lti` block, no environment has one yet, and admin-dash shows nothing Canvas.

## Decisions

- **Off without its key.** The tool's signing key reaches the module only through its config, filled from Secrets Manager. Without a usable key the LTI endpoints are not registered at all and the rest of the module starts as normal; nothing generates or falls back to a key.
- **Dynamic Registration only, then an operator's approval.** A platform registers itself from its own configuration document, which must name an issuer and a registration endpoint on the same host it was fetched from (Instructure's own issuers may use Instructure's documented login host), so a document hosted anywhere cannot pose as another platform or receive the registration token. A new platform stays pending, with its logins and launches refused, until a server admin approves it; a deployment the registration didn't name is recorded on its first signed launch after approval.
- **Every launch is fully validated** against LTI 1.3 Core and the 1EdTech Security Framework. The state and nonce issued at login are bound to that browser by a cookie and used once, so a launch cannot be replayed or carried into another browser.
- **Platform URLs are untrusted.** Every fetch to a platform-supplied address honours the homeserver's proxy and private-address blocklist, follows no redirects, and is size- and time-capped. A launch naming an unknown signing key triggers at most one key refetch per interval, so made-up keys cannot turn the tool into a request amplifier.
- **Hand-offs are single-use tickets.** A launch hands the app or admin-dash, in the URL fragment so no server logs it, an opaque ticket that expires after ten minutes and is consumed once, whatever happens next; only its hash is stored. Where a launch goes depends on role and on whether the Canvas identity is already linked; a linked student with nothing new to confirm gets a short-lived one-time sign-in instead. A server admin account never gets one and goes to the normal sign-in, and is never a managed account, because a platform's say-so must never open or manage an admin account.
- **Linking needs the account's own sign-in.** An unbound ticket links the account whose token returns it; a bound ticket acts only for the account it was issued to. One Canvas identity links to one account, and one account to one identity per platform. A Canvas email is never treated as verified and never links or signs in an account; the import uses it only to add an Invited row.
- **Connect binds one Canvas course to one Pangea course**, only for the account the instructor launch resolved to and only for a course it administers, re-checked at connect. The Canvas course comes from the ticket, never from the request.
- **Import reads only what connect stored.** Admin rights are checked before any call to Canvas. The membership read uses one token request and one request per page, never retried, and the token goes only to the roster's own host. Only active learners are imported. An import never rewrites a row's email, and two imports that race converge on the same rows. No email is sent.
- **Nothing sensitive in logs.** Logs, error bodies and Sentry carry reason codes, platform and room ids, and counts: never a token, claim, ticket, key, Canvas user id or email. The query string is dropped from every logged LTI request.
