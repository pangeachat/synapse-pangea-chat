import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import IO, Any, Dict, Optional, Tuple, Union

import aiounittest
import psycopg2
import requests
import testing.postgresql
import yaml
from psycopg2.extensions import parse_dsn

logger = logging.getLogger(__name__)


class BaseSynapseE2ETest(aiounittest.AsyncTestCase):
    """Base class for Synapse E2E tests with shared infrastructure methods."""

    server_url = "http://localhost:8008"

    async def start_test_synapse(
        self,
        *,
        module_config: Optional[Dict[str, Any]] = None,
        synapse_config_overrides: Optional[Dict[str, Any]] = None,
    ) -> Tuple[
        testing.postgresql.Postgresql,
        str,
        str,
        subprocess.Popen,
        threading.Thread,
        threading.Thread,
    ]:
        """Start a test Synapse server backed by PostgreSQL.

        Returns (postgres, synapse_dir, config_path, server_process, stdout_thread, stderr_thread).
        """
        postgres: Optional[testing.postgresql.Postgresql] = None
        synapse_dir: Optional[str] = None
        server_process: Optional[subprocess.Popen] = None
        stdout_thread: Optional[threading.Thread] = None
        stderr_thread: Optional[threading.Thread] = None
        try:
            postgres, db_url = await self._start_postgres()

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
                    "config": module_config or {},
                }
            ]

            dsn_params = parse_dsn(db_url)
            config["database"] = {
                "name": "psycopg2",
                "args": dsn_params,
            }

            if synapse_config_overrides:
                for key, value in synapse_config_overrides.items():
                    config[key] = value

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

            def read_output(pipe: Union[IO[str], None]) -> None:
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

            max_wait_time = 10
            wait_interval = 1
            total_wait_time = 0
            server_ready = False
            while not server_ready and total_wait_time < max_wait_time:
                try:
                    response = requests.get(f"{self.server_url}/health", timeout=10)
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
                postgres,
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
            if postgres is not None:
                postgres.stop()
            raise e

    async def _start_postgres(
        self,
    ) -> Tuple[testing.postgresql.Postgresql, str]:
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
    ) -> None:
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

    async def login_user(self, user: str, password: str) -> Tuple[str, str]:
        """Returns (user_id, access_token)."""
        login_url = f"{self.server_url}/_matrix/client/v3/login"
        login_data = {
            "type": "m.login.password",
            "user": user,
            "password": password,
        }
        response = requests.post(login_url, json=login_data)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        access_token = response_json["access_token"]
        user_id = response_json["user_id"]
        self.assertIsInstance(access_token, str)
        self.assertIsInstance(user_id, str)
        return (user_id, access_token)

    def stop_synapse(
        self,
        *,
        server_process: Optional[subprocess.Popen] = None,
        stdout_thread: Optional[threading.Thread] = None,
        stderr_thread: Optional[threading.Thread] = None,
        synapse_dir: Optional[str] = None,
        postgres: Any = None,
    ) -> None:
        """Clean up Synapse server resources. Call in finally blocks."""
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
        if postgres is not None:
            postgres.stop()

    async def create_private_room(self, access_token: str) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = f"{self.server_url}/_matrix/client/v3/createRoom"
        create_room_data = {"visibility": "private", "preset": "private_chat"}
        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def create_private_room_knock_allowed_room(self, access_token: str) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = f"{self.server_url}/_matrix/client/v3/createRoom"
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {"join_rule": "knock"},
                }
            ],
        }
        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def invite_user_to_room(
        self, room_id: str, user_id: str, access_token: str
    ) -> bool:
        invite_url = f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/invite"
        invite_data = {"user_id": user_id}
        response = requests.post(
            invite_url,
            json=invite_data,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return response.status_code == 200

    async def accept_room_invitation(self, room_id: str, access_token: str) -> bool:
        join_url = f"{self.server_url}/_matrix/client/v3/join/{room_id}"
        response = requests.post(
            join_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return response.status_code == 200
