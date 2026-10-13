"""Bounded local/staging HTTP load probe; never accepts production.

Run with SYNAPSE_BASE_URL, SYNAPSE_AUTH_TOKEN, NOTICE_CMS_URL and
NOTICE_CMS_API_KEY. Staging additionally requires NOTICE_LOAD_ALLOW_STAGING=1.
The existing bot sends only to itself in a temporary private room.
"""

import copy
import csv
import json
import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from tests.test_notice_content import request_body


def run_probe(base, token, cms_url, cms_key, cms_admin_token=None):
    staging = base == "https://matrix.staging.pangea.chat"
    if staging:
        assert os.environ.get("NOTICE_LOAD_ALLOW_STAGING") == "1"
        assert cms_url == "https://api.staging.pangea.chat/cms"
        assert cms_admin_token, "Staging cleanup requires a CMS admin session"
    else:
        assert urlparse(base).hostname in {"127.0.0.1", "localhost"}
        assert urlparse(cms_url).hostname in {"127.0.0.1", "localhost"}
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token

    def call(method, path, body=None, expected=200):
        response = session.request(method, base + path, json=body, timeout=20)
        assert response.status_code == expected, (
            path,
            response.status_code,
            response.text,
        )
        return response.json()

    uid = call("GET", "/_matrix/client/v3/account/whoami")["user_id"]
    if staging:
        assert uid == "@bot:staging.pangea.chat"
    prefix = "notice-228-integration-http-" + uuid.uuid4().hex + "-"
    endpoint = "/_synapse/client/pangea/v1/deliver_notice"
    room = call(
        "POST",
        "/_matrix/client/v3/createRoom",
        {"preset": "private_chat", "name": prefix},
    )["room_id"]
    room_path = "/_matrix/client/v3/rooms/" + quote(room, safe="")
    receipts = []
    try:
        call(
            "PUT",
            room_path + "/state/pangea.activity_plan/",
            {"activity_id": prefix, "roles": {"one": {}}},
        )
        call("PUT", room_path + "/state/pangea.activity_roles/", {})
        body = request_body("in-app-only")
        body.pop("notice_event_id")
        body.pop("email")
        body.pop("push")
        due = time.time() + 60
        body.update(
            user_id=uid,
            sender_id=uid,
            notice_room_id=room,
            activity_id=prefix,
            session_room_id=room,
            notice_content={"body": "Isolated scheduler load probe"},
            scheduled_at=datetime.fromtimestamp(due, timezone.utc).isoformat(),
            eligibility={"session_available": True, "min_contact_spacing_ms": 1},
        )
        body["log"]["run"]["decided_at"] = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory(prefix="notice-http-load-") as temp:
            fixture = Path(temp) / "fixture.json"
            receipt_file = Path(temp) / "receipts.jsonl"
            receipt_file.touch()
            report = Path("/tmp") / prefix
            fixture.write_text(
                json.dumps(
                    {
                        "url": base,
                        "token": token,
                        "bodies": [body],
                        "count": 100,
                        "prefix": prefix,
                        "receipts": str(receipt_file),
                        "stop_at": due + 70,
                    }
                )
            )
            fixture.chmod(0o600)
            try:
                with report.with_suffix(".log").open("w") as log:
                    result = subprocess.run(
                        [
                            "uv",
                            "tool",
                            "run",
                            "--from",
                            "locust==2.42.0",
                            "locust",
                            "-f",
                            "tests/load_notice_schedule.py",
                            "--headless",
                            "-u",
                            "4",
                            "-r",
                            "4",
                            "--run-time",
                            "140s",
                            "--csv",
                            str(report),
                        ],
                        env={**os.environ, "NOTICE_LOAD_FIXTURE": str(fixture)},
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=160,
                    )
                assert result.returncode == 0, report.with_suffix(".log").read_text()
            finally:
                receipts = [
                    json.loads(line) for line in receipt_file.read_text().splitlines()
                ]
        assert len(receipts) == 100, len(receipts)
        outcomes = []
        for receipt in receipts:
            state = call("GET", endpoint + "?schedule_id=" + receipt["schedule_id"])
            if receipt["index"] % 5 == 0:
                assert state["status"] == "cancelled", state
                repeated = call(
                    "DELETE", endpoint + "?schedule_id=" + receipt["schedule_id"]
                )
                assert repeated["status"] == "cancelled"
            else:
                assert state["status"] == "complete", state
                assert state["result"]["channel"] == "in_app", state
                assert state["result"]["log_status"] == "complete", state
                outcomes.append(state["result"])
            time.sleep(0.15)
        assert len({r["notification_log_id"] for r in outcomes}) == 80
        events = call("GET", room_path + "/messages?dir=b&limit=100")["chunk"]
        notices = [event for event in events if event["type"] == "p.room.notice"]
        assert len(notices) == 80, len(notices)
        assert all(event["origin_server_ts"] >= int(due * 1000) for event in notices)
        for condition, reason in (
            ({"recipient_not_returned": True}, "recipient_returned"),
            ({"min_contact_spacing_ms": 86400000}, "contact_spacing"),
        ):
            guarded = copy.deepcopy(body)
            guarded["eligibility"] = condition
            guarded["scheduled_at"] = datetime.fromtimestamp(
                time.time() + 2, timezone.utc
            ).isoformat()
            guarded["log"]["run"].update(
                run_id=prefix + reason, decided_at="2000-01-01T00:00:00Z"
            )
            queued = call("POST", endpoint, guarded, 202)
            receipts.append({"schedule_id": queued["schedule_id"]})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = call("GET", endpoint + "?schedule_id=" + queued["schedule_id"])
                if state["status"] == "complete":
                    break
                time.sleep(1)
            assert state["status"] == "complete", state
            assert state["result"]["reason"] == reason, state
            assert not state["result"].get("notice_event_id"), state
            assert state["result"]["log_status"] == "complete", state
        with Path(str(report) + "_stats.csv").open() as stream:
            measurements = list(csv.DictReader(stream))
        assert all(int(row["Failure Count"]) == 0 for row in measurements), measurements
        foreground = next(
            row for row in measurements if row["Name"] == "foreground sync"
        )
        assert int(foreground["Request Count"]) > 100, foreground
        assert float(foreground["95%"]) < (1000 if staging else 250), foreground
        assert float(foreground["Max Response Time"]) < 5000, foreground
        print(
            json.dumps(
                {
                    "environment": base,
                    "delivered": 80,
                    "cancelled": 20,
                    "sync_p95_ms": foreground["95%"],
                    "sync_max_ms": foreground["Max Response Time"],
                    "report": str(report) + "_stats.csv",
                }
            ),
            flush=True,
        )
        return report
    finally:
        for receipt in receipts:
            response = session.delete(
                base + endpoint,
                params={"schedule_id": receipt["schedule_id"]},
                timeout=20,
            )
            assert response.status_code in {200, 409}, response.text
            time.sleep(0.12)
        # The queue retains its deduplication receipt by design, but the temporary
        # room and test CMS records are removed. No real recipient was contacted.
        response = session.delete(
            base + "/_synapse/admin/v1/rooms/" + quote(room, safe=""),
            json={"purge": True, "block": False},
            timeout=30,
        )
        assert response.status_code == 200, response.text
        if cms_admin_token:
            response = requests.delete(
                cms_url + "/api/notification-log",
                headers={"Authorization": "JWT " + cms_admin_token},
                params={"where[run.run_id][like]": prefix},
                timeout=30,
            )
            assert response.status_code == 200, response.text
            assert not response.json().get("errors"), response.text
        # Local CMS fixture teardown owns its records and temporary service user.
        session.close()


if __name__ == "__main__":
    run_probe(
        os.environ["SYNAPSE_BASE_URL"],
        os.environ["SYNAPSE_AUTH_TOKEN"],
        os.environ["NOTICE_CMS_URL"],
        os.environ["NOTICE_CMS_API_KEY"],
        os.environ.get("NOTICE_CMS_ADMIN_TOKEN"),
    )
