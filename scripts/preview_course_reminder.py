"""Render the shipped course claim reminder; optionally serve a reloadable local preview.

No credentials or mail transport. --data takes a JSON object with the endpoint's
fields (subject, body, cta_label) and an optional video {url, title}. The claim
link is an inert local placeholder. --youtube-thumbnail draws a play button onto
the video's YouTube thumbnail and uses that file, for previewing only.
"""

import argparse
import io
import json
import re
import sys
import urllib.request
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "synapse_pangea_chat/email_invite/templates"
sys.path.insert(0, str(ROOT))

THUMBNAIL_FILE = "video-thumbnail.png"
DEFAULTS = {
    "app_name": "Pangea Chat",
    "subject": "Your course is waiting",
    "body": "Your course is ready for you to open.",
    "cta_label": "Open your course",
    "claim_url": "http://127.0.0.1/preview-only/claim",
    "video": None,
}


def load_values(data_path: Path | None) -> dict:
    values = dict(DEFAULTS)
    if data_path:
        values.update(json.loads(data_path.read_text()))
    return values


def render(values: dict) -> tuple[str, str]:
    from synapse_pangea_chat.email_invite.course_claim_emails import (
        reminder_paragraphs,
    )

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(),
        undefined=StrictUndefined,
    )
    template_vars = {**values, "paragraphs": reminder_paragraphs(values["body"])}
    return tuple(
        env.get_template("course_reminder." + extension).render(**template_vars)
        for extension in ("html", "txt")
    )


def youtube_id(url: str) -> str:
    match = re.search(r"(?:youtu\.be/|v=|/embed/|/shorts/)([\w-]{11})", url)
    if not match:
        raise ValueError(f"Not a YouTube video URL: {url}")
    return match.group(1)


def youtube_thumbnail_with_play_button(url: str) -> bytes:
    from PIL import Image, ImageDraw

    video_id = youtube_id(url)
    with urllib.request.urlopen(
        f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    ) as response:
        image = Image.open(io.BytesIO(response.read())).convert("RGBA")
    image = image.resize((1040, 585))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Low and centred: YouTube thumbnails often carry title text mid-frame.
    cx, cy, r = image.width // 2, int(image.height * 0.84), 52
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(94, 58, 207, 235))
    draw.polygon(
        [(cx - 16, cy - 26), (cx - 16, cy + 26), (cx + 28, cy)],
        fill=(255, 255, 255, 255),
    )
    out = io.BytesIO()
    Image.alpha_composite(image, overlay).convert("RGB").save(out, "PNG")
    return out.getvalue()


class PreviewHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, data_path=None, output=None, **kwargs):
        self.data_path = data_path
        self.output = output
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/" + THUMBNAIL_FILE:
            self._reply((self.output / THUMBNAIL_FILE).read_bytes(), "image/png")
            return
        if self.path not in ("/", "/email.html", "/email.txt"):
            self.send_error(404, "Preview-only link; no action performed")
            return
        try:
            html, plain = render(load_values(self.data_path))
        except Exception as error:
            self.log_error("Render failed: %s", error)
            self.send_error(500, "Template render failed; see terminal")
            return
        if self.path == "/email.txt":
            self._reply(plain.encode(), "text/plain; charset=utf-8")
        else:
            self._reply(html.encode(), "text/html; charset=utf-8")

    def _reply(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/pangea-course-reminder-preview")
    )
    parser.add_argument("--youtube-thumbnail", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    values = load_values(args.data)
    if args.youtube_thumbnail:
        if not values["video"]:
            parser.error("--youtube-thumbnail needs a video in --data")
        (args.output / THUMBNAIL_FILE).write_bytes(
            youtube_thumbnail_with_play_button(values["video"]["url"])
        )
    html, plain = render(values)
    (args.output / "email.html").write_text(html)
    (args.output / "email.txt").write_text(plain)
    print(f"Preview: {(args.output / 'email.html').resolve()}", flush=True)
    if args.serve:
        handler = partial(PreviewHandler, data_path=args.data, output=args.output)
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
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
