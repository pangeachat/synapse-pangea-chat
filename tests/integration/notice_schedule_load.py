"""Provision isolated Synapse/Postgres and run the requested Locust queue ladder.

NOTICE_CMS_FIXTURE=/tmp/notice-cms.json python -m unittest tests.integration.notice_schedule_load
NOTICE_LOAD_COUNTS defaults to 100,500,1000. No production URLs are accepted.
"""

import copy
import csv
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import requests

from tests.base_e2e import BaseSynapseE2ETest
from tests.test_notice_content import request_body


class TestNoticeScheduleLoad(BaseSynapseE2ETest):
    async def test_queue_ladder(self):
        cms = json.loads(Path(os.environ["NOTICE_CMS_FIXTURE"]).read_text())
        self.assertTrue(cms["url"].startswith("http://127.0.0.1:"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.server_url = f"http://127.0.0.1:{port}"
        started = await self.start_test_synapse(
            module_config={
                "cms_base_url": cms["url"],
                "cms_service_api_key": cms["api_key"],
                # Stress the worker beyond the public admin admission defaults.
                "notice_admin_requests_per_minute": 60000,
                "notice_admin_burst": 2000,
            },
            synapse_config_overrides={
                "rc_message": {"per_second": 100, "burst_count": 100},
                "rc_room_creation": {"per_second": 100, "burst_count": 100},
                "rc_joins": {
                    name: {"per_second": 100, "burst_count": 100}
                    for name in ("local", "remote", "per_room")
                },
                "rc_invites": {
                    name: {"per_second": 100, "burst_count": 100}
                    for name in ("per_room", "per_user", "per_issuer")
                },
                "listeners": [
                    {
                        "port": port,
                        "type": "http",
                        "tls": False,
                        "bind_addresses": ["127.0.0.1"],
                        "resources": [{"names": ["client"], "compress": False}],
                    }
                ],
            },
        )
        postgres, directory, config_path, process, stdout, stderr = started
        try:
            await self.register_user(
                config_path, directory, "loadadmin", "pw", admin=True
            )
            _, token = await self.login_user("loadadmin", "pw")
            headers = {"Authorization": "Bearer " + token}
            if os.environ.get("NOTICE_HTTP_PROBE_ONLY") == "1":
                from tests.staging_tests.notice_schedule import run_probe

                run_probe(self.server_url, token, cms["url"], cms["api_key"])
                return
            bodies = []
            for index in range(32):
                uid = f"@notice_load_{index}:my.domain.name"
                response = requests.put(
                    self.server_url + "/_synapse/admin/v2/users/" + uid,
                    headers=headers,
                    json={"password": "fixture-only"},
                    timeout=20,
                )
                self.assertEqual(response.status_code, 201, response.text)
                response = requests.post(
                    self.server_url + "/_matrix/client/v3/createRoom",
                    headers=headers,
                    json={"preset": "private_chat", "invite": [uid]},
                    timeout=20,
                )
                self.assertEqual(response.status_code, 200, response.text)
                room = response.json()["room_id"]
                response = requests.post(
                    self.server_url + "/_synapse/admin/v1/join/" + room,
                    headers=headers,
                    json={"user_id": uid},
                    timeout=20,
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = request_body("in-app-only")
                del body["notice_event_id"]
                body.update(
                    user_id=uid,
                    notice_room_id=room,
                    sender_id="@loadadmin:my.domain.name",
                    notice_content={"body": "Local load fixture"},
                    eligibility={
                        "recipient_not_returned": True,
                        "activity_not_started": True,
                        "min_contact_spacing_ms": 1,
                    },
                )
                bodies.append(body)
            connection = psycopg2.connect(self.database_url)
            self.addCleanup(connection.close)
            connection.autocommit = True
            for count in map(
                int, os.environ.get("NOTICE_LOAD_COUNTS", "100,500,1000").split(",")
            ):
                prefix = "notice-228-integration-load-" + uuid.uuid4().hex + "-"
                due = time.time() + 30
                rung_bodies = copy.deepcopy(bodies)
                for body in rung_bodies:
                    body["scheduled_at"] = datetime.fromtimestamp(
                        due, timezone.utc
                    ).isoformat()
                with tempfile.TemporaryDirectory(prefix="notice-load-") as output:
                    fixture = Path(output) / "fixture.json"
                    fixture.write_text(
                        json.dumps(
                            {
                                "url": self.server_url,
                                "token": token,
                                "bodies": rung_bodies,
                                "count": count,
                                "prefix": prefix,
                                "stop_at": due + 150,
                            }
                        )
                    )
                    fixture.chmod(0o600)
                    report = Path("/tmp") / (prefix + str(count))
                    log = report.with_suffix(".log")
                    with log.open("w") as stream:
                        load = subprocess.Popen(
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
                                "11",
                                "-r",
                                "11",
                                "--run-time",
                                "190s",
                                "--csv",
                                str(report),
                                "--exit-code-on-error",
                                "1",
                            ],
                            env={**os.environ, "NOTICE_LOAD_FIXTURE": str(fixture)},
                            stdout=stream,
                            stderr=subprocess.STDOUT,
                        )
                        try:
                            deadline = time.monotonic() + 210
                            complete_at = None
                            max_rss_kib = 0
                            while load.poll() is None and time.monotonic() < deadline:
                                time.sleep(1)
                                rss = subprocess.check_output(
                                    ["ps", "-o", "rss=", "-p", str(process.pid)],
                                    text=True,
                                )
                                max_rss_kib = max(max_rss_kib, int(rss.strip()))
                                if time.time() < due:
                                    continue
                                with connection.cursor() as cursor:
                                    cursor.execute(
                                        "SELECT COUNT(*) FROM pangea_notice_schedule WHERE decision_key LIKE %s AND status IN ('complete', 'cancelled')",
                                        ("%" + prefix + "%",),
                                    )
                                    terminal = cursor.fetchone()[0]
                                if terminal == count:
                                    complete_at = complete_at or time.monotonic()
                                    if time.monotonic() - complete_at > 10:
                                        load.send_signal(signal.SIGINT)
                                        load.wait(timeout=15)
                                        break
                        finally:
                            if load.poll() is None:
                                load.terminate()
                                load.wait(timeout=10)
                    self.assertEqual(load.returncode, 0, log.read_text()[-5000:])
                    with Path(str(report) + "_stats.csv").open() as stats:
                        measurements = list(csv.DictReader(stats))
                    foreground = next(
                        r for r in measurements if r["Name"] == "foreground sync"
                    )
                    self.assertGreater(int(foreground["Request Count"]), 100)
                    self.assertTrue(
                        all(int(r["Failure Count"]) == 0 for r in measurements)
                    )
                    # A regression budget for this local fixture, not a claim
                    # about a deployed server's hardware or production traffic.
                    self.assertLess(float(foreground["95%"]), 250)
                    self.assertLess(float(foreground["Max Response Time"]), 2000)
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT status, COUNT(*) FROM pangea_notice_schedule WHERE decision_key LIKE %s GROUP BY status",
                            ("%" + prefix + "%",),
                        )
                        statuses = dict(cursor.fetchall())
                        cursor.execute(
                            "SELECT result FROM pangea_notice_schedule WHERE decision_key LIKE %s AND status = 'complete'",
                            ("%" + prefix + "%",),
                        )
                        outcomes = [json.loads(row[0]) for row in cursor.fetchall()]
                        cursor.execute(
                            "SELECT MAX(e.origin_server_ts - s.scheduled_at_ms) FROM pangea_notice_schedule s JOIN events e ON e.event_id = (s.result::jsonb ->> 'notice_event_id') WHERE s.decision_key LIKE %s",
                            ("%" + prefix + "%",),
                        )
                        max_delay_ms = cursor.fetchone()[0]
                    self.assertEqual(
                        statuses,
                        {"cancelled": count // 5, "complete": count - count // 5},
                    )
                    self.assertTrue(
                        all(
                            row.get("channel") == "in_app"
                            and row.get("log_status") == "complete"
                            for row in outcomes
                        ),
                        outcomes[:5],
                    )
                    print(
                        f"Notice load {count}: {statuses}; max send lateness {max_delay_ms}ms; sync p95 {foreground['95%']}ms; sync max {foreground['Max Response Time']}ms; Synapse peak RSS {max_rss_kib}KiB; report {report}_stats.csv",
                        flush=True,
                    )
        finally:
            self.stop_synapse(
                server_process=process,
                stdout_thread=stdout,
                stderr_thread=stderr,
                synapse_dir=directory,
                postgres=postgres,
            )
