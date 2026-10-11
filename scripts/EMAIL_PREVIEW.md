# Local notice email iteration

Use the repo's development `.venv` (setup in `.github/instructions/testing.instructions.md`). No account credentials, AWS access, deployment, or external mail delivery is needed.

## Fast visual loop

```sh
.venv/bin/python scripts/preview_notice_email.py --serve
```

Open http://127.0.0.1:8765 and refresh after editing a template. The server reads the shipped Jinja templates on each request, binds only to loopback, and serves HTML at `/email.html` and plain text at `/email.txt`. Ctrl-C stops it. `--port 0` chooses an available port and prints it.

Use `--data /path/to/copy.json` to override `title`, `body`, `cta_label`, `category_label`, or other template values. Changes to that JSON also appear on refresh. The default CTA is an inert local link. The Unsubscribe link opens `/unsubscribe`, rendering the actual confirmation template. Submitting either choice returns a preview-only message and never changes preferences. Without `--serve`, the command writes `email.html` and `email.txt` to `/tmp/pangea-notice-preview`; choose another directory with `--output`.

Browser previews cover layout, responsive CSS, and the browser's light/dark appearance. They do not emulate Gmail or Outlook's HTML transformations.

## Real local delivery check

```sh
PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:$PATH" \
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH="$PWD" \
NOTICE_EMAIL_CAPTURE_DIR=/tmp/pangea-notice-capture \
.venv/bin/python -m unittest tests.test_notice_email_smtp
```

This test starts an isolated local Synapse and PostgreSQL, creates disposable local users, invokes `deliver_notice`, and captures the actual SMTP message on loopback. It verifies HTML and plain-text parts, brand assets, address, signed CTA, unsubscribe headers, the harmless GET confirmation, and the refusal POST. Both server and test database are torn down. It uses an available HTTP port rather than occupying the local stack's port 8008.

The optional output directory contains `email.eml`, `email.html`, and `email.txt`. Open the `.eml` in a mail client to inspect the captured MIME. Its signed URLs target the temporary test server and stop working after teardown. The default recipient is `preview@example.test`, and SMTP never relays externally.

## Updating the brand shell

The brand email layout's source is in synapse-templates; its design and drift check are in that repo's `brand-email-layout.instructions.md`. The `_brand_*` templates in this package are generated from it, so a brand change is made there and regenerated here:

```sh
.venv/bin/python ../synapse-templates/scripts/brand_email.py write module .
.venv/bin/python ../synapse-templates/scripts/brand_email.py check module . --ref origin/main
```

Notice-specific content lives in `notice_email.html`. The preview's default postal address comes from the layout's footer; deployed delivery continues to use its required configuration value.
