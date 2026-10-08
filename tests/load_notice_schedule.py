"""Locust schedule/cancel/drain workload; fixture contains synthetic local users.

Run via tests.integration.notice_schedule_load, which owns fixture cleanup.
Real Synapse, PostgreSQL and CMS; in-app delivery avoids external mail traffic.
"""

import copy
import itertools
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from locust import HttpUser, between, task
from locust.exception import StopUser

fixture = json.loads(Path(os.environ["NOTICE_LOAD_FIXTURE"]).read_text())
staging = (
    fixture["url"] == "https://matrix.staging.pangea.chat"
    and os.environ.get("NOTICE_LOAD_ALLOW_STAGING") == "1"
    and fixture["count"] <= 100
)
if not staging and urlparse(fixture["url"]).hostname not in {"127.0.0.1", "localhost"}:
    raise ValueError("This load scenario requires an isolated local homeserver")
counter = itertools.count()


class ScheduledNoticeUser(HttpUser):
    host = fixture["url"]
    wait_time = between(0.8, 1.2) if staging else between(0.05, 0.1)

    def on_start(self):
        self.client.headers["Authorization"] = "Bearer " + fixture["token"]

    @task
    def schedule(self):
        index = next(counter)
        if index >= fixture["count"]:
            raise StopUser()
        body = copy.deepcopy(fixture["bodies"][index % len(fixture["bodies"])])
        body["log"]["run"]["run_id"] = fixture["prefix"] + str(index)
        path = "/_synapse/client/pangea/v1/deliver_notice"
        with self.client.post(
            path, json=body, name="enqueue", catch_response=True
        ) as response:
            if response.status_code != 202:
                response.failure("enqueue status " + str(response.status_code))
                return
            schedule_id = response.json()["schedule_id"]
        if fixture.get("receipts"):
            with open(fixture["receipts"], "a") as receipt:
                receipt.write(
                    json.dumps({"index": index, "schedule_id": schedule_id}) + "\n"
                )
        status_path = path + "?schedule_id=" + schedule_id
        if index % 5 == 0:
            with self.client.delete(
                status_path, name="cancel", catch_response=True
            ) as response:
                if (
                    response.status_code != 200
                    or response.json().get("status") != "cancelled"
                ):
                    response.failure("cancellation did not win before due")
        # Polling every job would overwhelm the endpoint with test traffic.
        # The runner independently verifies every terminal result in Postgres.


class ForegroundUser(HttpUser):
    host = fixture["url"]
    fixed_count = 1
    wait_time = between(0.5, 0.7) if staging else between(0.1, 0.2)

    def on_start(self):
        self.client.headers["Authorization"] = "Bearer " + fixture["token"]
        self.since = None

    @task
    def sync(self):
        params = {"timeout": 0}
        if self.since:
            params["since"] = self.since
        with self.client.get(
            "/_matrix/client/v3/sync",
            params=params,
            name="foreground sync",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                self.since = response.json()["next_batch"]
        if time.time() > fixture["stop_at"]:
            self.environment.runner.quit()
