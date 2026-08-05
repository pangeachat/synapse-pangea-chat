---
applyTo: "synapse_pangea_chat/find_user_by_email/**,tests/test_find_user_by_email_e2e.py,tests/test_find_user_by_email_unit.py"
description: "Server-admin-only lookup that answers 'which account, if any, has this email address?' — access bar, exact-match rule, and what a match reveals."
---

# Find User By Email — Synapse Module

Contact and support work starts from an email address far more often than from a Matrix handle. A teacher writes in, a conference contact asks about their sign-up, a research participant needs their account checked — and the first question is always "do they have a Pangea Chat account?"

Synapse cannot answer it. Its admin user search matches only the user-ID localpart and the display name, never the email addresses users register with. So today the answer is a guess unless the person's handle happens to contain their email. This module closes that gap.

The first consumer is the `lm whois` contact-lookup tool in `admin/email-marketing`, whose Matrix section currently has to disclaim that email lookups don't work.

## Who may call it

**Server admins only.** A non-admin caller is refused, whether or not a match exists.

The bar is high because the endpoint is, by construction, an oracle: anyone who can call it can test any address in the world for "is this person a Pangea Chat user?" That is a privacy disclosure about our users to whoever holds the token, so it sits at the same level as the module's other privileged operations (delete user, export user data). Calls are rate limited per caller, so a leaked admin token still cannot sweep a mailing list quickly.

## Contract

- **Request** — one email address.
- **Response** — a list of matches. Each match carries the account's user ID, its display name, whether the account is deactivated, and the address exactly as stored.
- **No match** — an empty list, not an error. "This address has no account" is a successful answer to the question, and the caller should not have to distinguish it from a failure.

The response deliberately stops at identity and account status. Anything further about the account is a separate, already-existing admin call.

## Matching rules

| Rule | Behavior | Why |
| --- | --- | --- |
| Medium | Only `email` addresses are searched. | Phone numbers are a different lookup with different consent expectations. |
| Case | Case-insensitive, both sides. | Support staff paste addresses as they were written to them, so the caller's spelling is arbitrary. Synapse lower-cases addresses when it binds them, which handles the stored side for anything it wrote; comparing case-insensitively on both sides also covers rows written by other tooling. A lookup that missed `Person@School.edu` would report "no account" for a real user — the worst failure this endpoint can have. |
| Precision | Whole address only — no partial, wildcard, or domain search. | A domain search would enumerate every account at a school from one call. Exact match keeps the caller to addresses they already have. |
| Scope | Local accounts only. | The homeserver only stores addresses for its own users. |
| Account status | Each match reports whether the account is deactivated. | Support needs to distinguish a live account from a closed one; the two lead to completely different replies. |

### What "no match" does and does not mean

Synapse removes an account's email bindings when the account is deactivated. A closed account therefore has nothing left for this lookup to find, so **"no match" means "no live account with this address" — not "this person never signed up."** Anyone reading a negative result, human or tool, has to treat it that way; a deactivated account is a real support case that this endpoint cannot see.

The deactivated flag on a match is still part of the contract, because the binding can outlive the account in edge cases (a partially failed deactivation, or a row written outside the normal flow). It is a correctness guard, not the common path.

## An address belongs to at most one account

Synapse enforces one account per email address, and binding an address that another account already holds moves it rather than duplicating it. So a lookup returns zero or one match — never two. The duplicate-registration case an operator might expect to see here cannot exist under the same address: a second sign-up needs a different one (`person+class@school.edu`), which is a different address and will not match.

The response is still a list. It keeps "no account" and "an account" the same shape for the caller, and it means the contract survives unchanged if Synapse ever relaxes that constraint.

## Non-goals

- **Reverse lookup** (account → its addresses). Synapse's own admin user API already returns a user's threepids.
- **Changing anything.** Read-only; binding and unbinding addresses stay with Synapse.
- **Unverified addresses.** Only addresses actually bound to an account are searched, so a match means the person proved control of that address.
