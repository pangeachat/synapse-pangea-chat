"""Render the shipped nudge templates; optionally serve reloadable local previews.

No credentials or mail transport. --data accepts a JSON object of template values.
"""

import argparse
import json
import sys
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "synapse_pangea_chat/nudge_delivery/templates"
sys.path.insert(0, str(ROOT))


def template_context(data_path: Path | None = None):
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(),
        undefined=StrictUndefined,
    )
    values = {
        "app_name": "Pangea Chat",
        "title": "Ready for your next conversation?",
        "body": "A little practice goes a long way. Start a conversation and try something new today.",
        "cta_label": "Open Pangea Chat",
        "cta_url": "http://127.0.0.1/preview-only/cta",
        "unsubscribe_url": "/unsubscribe",
        "category_label": "conversation and course suggestions",
    }
    values["postal_address"] = (
        env.get_template("brand_base.html").make_module(values).brand_postal_address
    )
    if data_path:
        values.update(json.loads(data_path.read_text()))
    return env, values


def render(data_path: Path | None = None) -> tuple[str, str]:
    env, values = template_context(data_path)
    return tuple(
        env.get_template("nudge_email." + extension).render(**values)
        for extension in ("html", "txt")
    )


class PreviewHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, data_path=None, **kwargs):
        self.data_path = data_path
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path not in ("/", "/email.html", "/email.txt", "/unsubscribe"):
            self.send_error(404, "Preview-only link; no action performed")
            return
        try:
            html, plain = render(self.data_path)
            if self.path == "/unsubscribe":
                env, values = template_context(self.data_path)
                from synapse_pangea_chat.nudge_delivery.common import preference_rows

                html = env.get_template("nudge_unsubscribe_confirm.html").render(
                    **values,
                    token="local-preview-only",
                    preference_rows=preference_rows({}),
                    all_off=False,
                )
        except Exception as error:
            self.log_error("Render failed: %s", error)
            self.send_error(500, "Template render failed; see terminal")
            return
        plain_requested = self.path == "/email.txt"
        body = (plain if plain_requested else html).encode()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
            if plain_requested
            else "text/html; charset=utf-8",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_error(405, "Local preview only: no email preferences were changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/pangea-nudge-preview")
    )
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    html, plain = render(args.data)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "email.html").write_text(html)
    (args.output / "email.txt").write_text(plain)
    print(f"Preview: {(args.output / 'email.html').resolve()}", flush=True)
    if args.serve:
        with ThreadingHTTPServer(
            ("127.0.0.1", args.port), partial(PreviewHandler, data_path=args.data)
        ) as server:
            print(
                f"Live preview: http://127.0.0.1:{server.server_port} (refresh after editing; Ctrl-C to stop)",
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                # silent-ok: Ctrl-C is the documented normal shutdown action.
                pass


if __name__ == "__main__":
    main()
