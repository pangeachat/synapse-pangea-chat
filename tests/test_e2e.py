import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO, Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

import aiounittest
import psycopg2
import requests
import testing.postgresql
import yaml
from psycopg2.extensions import parse_dsn

from synapse_pangea_chat.get_public_courses import _cache
from synapse_pangea_chat.is_rate_limited import request_log as rate_limit_log

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="w",
    force=True,
)


class TestE2E(aiounittest.AsyncTestCase):
    async def start_test_synapse(
        self,
        *,
        postgresql_url: str,
        db: Optional[testing.postgresql.Postgresql] = None,
    ) -> Tuple[str, str, subprocess.Popen, threading.Thread, threading.Thread]:
        synapse_dir: Optional[str] = None
        server_process: Optional[subprocess.Popen] = None
        stdout_thread: Optional[threading.Thread] = None
        stderr_thread: Optional[threading.Thread] = None
        try:
            synapse_dir = tempfile.mkdtemp()
            config_path = os.path.join(synapse_dir, "homeserver.yaml")
            generate_config_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--server-name=my.domain.name",
                f"--config-path={config_path}",
                "--report-stats=no",
                "--generate-config",
            ]
            subprocess.check_call(generate_config_cmd)
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            log_config_path = config.get("log_config")
            config["modules"] = [
                {
                    "module": "synapse_pangea_chat.PangeaChat",
                    "config": {},
                }
            ]
            roomdirectory_config = config.setdefault("roomdirectory", {})
            roomdirectory_config["enable_room_list_search"] = True
            roomdirectory_config["room_list_publication_rules"] = [
                {
                    "action": "allow",
                    "user_id": "*",
                    "room_id": "*",
                    "alias": "*",
                }
            ]
            dsn_params = parse_dsn(postgresql_url)
            config["database"] = {
                "name": "psycopg2",
                "args": dsn_params,
            }
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f)
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            with open(log_config_path, "r", encoding="utf-8") as f:
                log_config = yaml.safe_load(f)
            log_config["root"]["handlers"] = ["console"]
            log_config["root"]["level"] = "DEBUG"
            with open(log_config_path, "w", encoding="utf-8") as f:
                yaml.dump(log_config, f)
            run_server_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--config-path",
                config_path,
            ]
            server_process = subprocess.Popen(
                run_server_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=synapse_dir,
                text=True,
            )

            def read_output(pipe: Union[IO[str], None]):
                if pipe is None:
                    return
                for line in iter(pipe.readline, ""):
                    logger.debug(line)
                pipe.close()

            stdout_thread = threading.Thread(
                target=read_output, args=(server_process.stdout,)
            )
            stderr_thread = threading.Thread(
                target=read_output, args=(server_process.stderr,)
            )
            stdout_thread.start()
            stderr_thread.start()
            server_url = "http://localhost:8008"
            max_wait_time = 10
            wait_interval = 1
            total_wait_time = 0
            server_ready = False
            while not server_ready and total_wait_time < max_wait_time:
                try:
                    response = requests.get(server_url, timeout=10)
                    if response.status_code == 200:
                        server_ready = True
                        break
                except requests.exceptions.ConnectionError:
                    pass
                finally:
                    await asyncio.sleep(wait_interval)
                    total_wait_time += wait_interval
            if not server_ready:
                raise RuntimeError("Synapse server did not start successfully")
            return (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            )
        except Exception as e:
            if server_process is not None:
                server_process.terminate()
                try:
                    server_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server_process.kill()
                    server_process.wait(timeout=30)
            if stdout_thread is not None:
                stdout_thread.join(timeout=10)
            if stderr_thread is not None:
                stderr_thread.join(timeout=10)
            if synapse_dir is not None and os.path.exists(synapse_dir):
                shutil.rmtree(synapse_dir)
            if db is not None:
                db.stop()
            raise e

    async def start_test_postgres(self):
        postgresql = None
        try:
            postgresql = testing.postgresql.Postgresql()
            postgres_url = postgresql.url()
            max_waiting_time = 10
            wait_interval = 1
            total_wait_time = 0
            postgres_is_up = False
            while total_wait_time < max_waiting_time and not postgres_is_up:
                try:
                    conn = psycopg2.connect(postgres_url)
                    conn.close()
                    postgres_is_up = True
                    break
                except psycopg2.OperationalError:
                    await asyncio.sleep(wait_interval)
                    total_wait_time += wait_interval
            if not postgres_is_up:
                postgresql.stop()
                self.fail("Postgres did not start successfully")
            dbname = "testdb"
            conn = psycopg2.connect(postgres_url)
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(
                f"""
                CREATE DATABASE {dbname}
                WITH TEMPLATE template0
                LC_COLLATE 'C'
                LC_CTYPE 'C';
            """
            )
            cursor.close()
            conn.close()
            dsn_params = parse_dsn(postgres_url)
            dsn_params["dbname"] = dbname
            postgres_url_testdb = psycopg2.extensions.make_dsn(**dsn_params)
            return postgresql, postgres_url_testdb
        except Exception as e:
            if postgresql is not None:
                postgresql.stop()
            raise e

    async def register_user(
        self, config_path: str, dir: str, user: str, password: str, admin: bool
    ):
        register_user_cmd = [
            "register_new_matrix_user",
            f"-c={config_path}",
            f"--user={user}",
            f"--password={password}",
        ]
        if admin:
            register_user_cmd.append("--admin")
        else:
            register_user_cmd.append("--no-admin")
        subprocess.check_call(register_user_cmd, cwd=dir)

    async def login_user(self, user: str, password: str) -> str:
        login_url = "http://localhost:8008/_matrix/client/v3/login"
        login_data = {
            "type": "m.login.password",
            "user": user,
            "password": password,
        }
        response = requests.post(login_url, json=login_data)
        self.assertEqual(response.status_code, 200)
        return response.json()["access_token"]

    async def test_public_courses_endpoint_returns_public_course(self):
        _cache.clear()
        rate_limit_log.clear()

        db = None
        synapse_dir = None
        config_path = None
        server_process: Optional[subprocess.Popen] = None
        stdout_thread: Optional[threading.Thread] = None
        stderr_thread: Optional[threading.Thread] = None

        try:
            db, postgres_url = await self.start_test_postgres()

            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                db=db,
                postgresql_url=postgres_url,
            )

            await self.register_user(
                config_path, synapse_dir, user="admin", password="adminpass", admin=True
            )
            await self.register_user(
                config_path,
                synapse_dir,
                user="student",
                password="studentpass",
                admin=False,
            )

            admin_token = await self.login_user("admin", "adminpass")

            base_url = "http://localhost:8008"
            headers = {"Authorization": f"Bearer {admin_token}"}

            alias_suffix = int(time.time())
            create_room_payload = {
                "name": "Course Alpha",
                "preset": "public_chat",
                "visibility": "public",
                "room_alias_name": f"course-alpha-{alias_suffix}",
            }
            create_response = requests.post(
                f"{base_url}/_matrix/client/v3/createRoom",
                json=create_room_payload,
                headers=headers,
                timeout=30,
            )
            self.assertEqual(
                create_response.status_code,
                200,
                msg=f"Failed to create room: {create_response.text}",
            )
            room_id = create_response.json()["room_id"]
            room_id_path = quote(room_id, safe="")

            directory_response = requests.put(
                f"{base_url}/_matrix/client/v3/directory/list/room/{room_id_path}",
                json={"visibility": "public"},
                headers=headers,
                timeout=30,
            )
            if directory_response.status_code not in (200, 202):
                if directory_response.status_code == 403:
                    logger.debug(
                        "Falling back to manual directory publish due to 403: %s",
                        directory_response.text,
                    )
                    conn = psycopg2.connect(postgres_url)
                    try:
                        with conn:
                            with conn.cursor() as cursor:
                                cursor.execute(
                                    "UPDATE rooms SET is_public = TRUE WHERE room_id = %s",
                                    (room_id,),
                                )
                    finally:
                        conn.close()
                else:
                    self.fail(
                        f"Failed to update directory visibility: {directory_response.text}"
                    )

            name_response = requests.put(
                f"{base_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.name",
                json={"name": "Course Alpha"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(name_response.status_code, 200)

            topic_response = requests.put(
                f"{base_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.topic",
                json={"topic": "Intro to Testing"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(topic_response.status_code, 200)

            join_rule_response = requests.put(
                f"{base_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.join_rules",
                json={"join_rule": "public"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(join_rule_response.status_code, 200)

            plan_response = requests.put(
                f"{base_url}/_matrix/client/v3/rooms/{room_id_path}/state/pangea.course_plan",
                json={"plan_id": "course-alpha", "modules": ["intro"]},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(plan_response.status_code, 200)

            payload = None
            matching_courses: List[Dict[str, Any]] = []
            for _ in range(10):
                public_courses_response = requests.get(
                    f"{base_url}/_synapse/client/unstable/org.pangea/public_courses",
                    headers=headers,
                    timeout=30,
                )
                if public_courses_response.status_code == 200:
                    payload = public_courses_response.json()
                    chunk = payload.get("chunk", [])
                    matching_courses = [
                        course for course in chunk if course["room_id"] == room_id
                    ]
                    if matching_courses:
                        break
                await asyncio.sleep(1)

            if not matching_courses:
                conn = psycopg2.connect(postgres_url)
                try:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT room_id, is_public FROM rooms WHERE room_id = %s",
                            (room_id,),
                        )
                        room_state = cursor.fetchall()
                        cursor.execute(
                            "SELECT type, state_key FROM state_events WHERE room_id = %s",
                            (room_id,),
                        )
                        state_types = cursor.fetchall()
                finally:
                    conn.close()
                self.fail(
                    f"Expected room {room_id} in public courses response, got {payload}. "
                    f"rooms table: {room_state}, state events: {state_types}"
                )

            course = matching_courses[0]
            self.assertEqual(course["name"], "Course Alpha")
            self.assertEqual(course["topic"], "Intro to Testing")

            log_file_path = None
            for handler in logging.getLogger().handlers:
                if hasattr(handler, "baseFilename"):
                    log_file_path = handler.baseFilename
                    break

            self.assertIsNotNone(log_file_path, "No log file handler configured")
            self.assertTrue(os.path.exists(log_file_path))

            with open(log_file_path, "r", encoding="utf-8") as log_file:
                log_contents = log_file.read()
            self.assertIn("Executing public courses query", log_contents)
            self.assertIn("pangea.course_plan", log_contents)
        finally:
            if server_process is not None:
                server_process.terminate()
                try:
                    server_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server_process.kill()
                    server_process.wait(timeout=30)
            if stdout_thread is not None:
                stdout_thread.join(timeout=10)
            if stderr_thread is not None:
                stderr_thread.join(timeout=10)
            if synapse_dir is not None and os.path.exists(synapse_dir):
                shutil.rmtree(synapse_dir)
            if db is not None:
                db.stop()
