import asyncio
import atexit
import gc
import hashlib
import hmac
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass, field
from typing import IO, Any, Dict, Optional, Tuple, Union, cast
from unittest.mock import patch

import aiounittest
import psycopg2
import requests
import testing.postgresql
import yaml
from psycopg2.extensions import parse_dsn

logger = logging.getLogger(__name__)

SERVER_NAME = "my.domain.name"


def _is_ignorable_resource_warning(warning: warnings.WarningMessage) -> bool:
    message = str(warning.message)
    return (
        warning.filename.endswith("asyncio/base_events.py")
        and "unclosed event loop" in message
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _config_key(
    module_config: Optional[Dict[str, Any]],
    synapse_config_overrides: Optional[Dict[str, Any]],
) -> str:
    return json.dumps(
        [module_config or {}, synapse_config_overrides or {}],
        sort_keys=True,
        default=str,
    )


class _Database:
    """One homeserver's database, in the PostgreSQL this process shares."""

    def __init__(self, admin_url: str, name: str) -> None:
        self._admin_url = admin_url
        self.name = name
        dsn = parse_dsn(admin_url)
        dsn["dbname"] = name
        self.url = psycopg2.extensions.make_dsn(**dsn)

    def stop(self) -> None:
        """Drop the database; a failure is logged, as teardown must not raise."""
        try:
            conn = psycopg2.connect(self._admin_url)
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS {self.name} WITH (FORCE)")
            finally:
                conn.close()
        except psycopg2.Error:
            logger.warning("Could not drop test database %s", self.name, exc_info=True)


_postgres: Optional[testing.postgresql.Postgresql] = None
_databases_created = 0


def _new_database() -> _Database:
    """A fresh, empty database for one homeserver.

    Starting PostgreSQL, initdb included, costs most of a second; a database
    in a running one costs milliseconds. So a test process starts one
    PostgreSQL on first use, stops it at exit, and gives every homeserver a
    database of its own in it.
    """
    global _postgres, _databases_created
    if _postgres is None:
        _postgres = testing.postgresql.Postgresql()
        atexit.register(_postgres.stop)
    _databases_created += 1
    database = _Database(_postgres.url(), f"homeserver{_databases_created}")
    conn = psycopg2.connect(_postgres.url())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE {database.name} WITH TEMPLATE template0 "
                "LC_COLLATE 'C' LC_CTYPE 'C'"
            )
    finally:
        conn.close()
    return database


_config_template: Optional[str] = None


def _write_generated_config(synapse_dir: str) -> str:
    """Write Synapse's generated config into `synapse_dir`; returns homeserver.yaml.

    Generating imports Synapse in a subprocess, a second or more, so a process
    generates once and gives each server a copy, with the paths that pointed
    into the template directory pointed into the server's own. The servers
    share keys and secrets, which matters to nothing a test does: each has its
    own database, so a token from one means nothing to another.
    """
    global _config_template
    if _config_template is None:
        template = tempfile.mkdtemp()
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                f"--server-name={SERVER_NAME}",
                f"--config-path={os.path.join(template, 'homeserver.yaml')}",
                "--report-stats=no",
                "--generate-config",
            ]
        )
        atexit.register(shutil.rmtree, template, True)
        _config_template = template
    for name in os.listdir(_config_template):
        source = os.path.join(_config_template, name)
        if not os.path.isfile(source):
            continue
        with open(source, "r", encoding="utf-8") as f:
            text = f.read()
        with open(os.path.join(synapse_dir, name), "w", encoding="utf-8") as f:
            f.write(text.replace(_config_template, synapse_dir))
    return os.path.join(synapse_dir, "homeserver.yaml")


def _stop(
    *,
    server_process: Optional[subprocess.Popen] = None,
    stdout_thread: Optional[threading.Thread] = None,
    stderr_thread: Optional[threading.Thread] = None,
    synapse_dir: Optional[str] = None,
    database: Optional[_Database] = None,
) -> None:
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
    if database is not None:
        database.stop()


@dataclass
class Homeserver:
    """One running test Synapse, its database, and where it lives."""

    url: str
    key: str
    config_path: str
    synapse_dir: str
    database_url: str
    registration_shared_secret: str
    database: _Database
    server_process: subprocess.Popen
    stdout_thread: threading.Thread
    stderr_thread: threading.Thread
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)

    def running(self) -> bool:
        return self.server_process.poll() is None

    def log_tail(self, lines: int = 20) -> str:
        return (
            "stdout_tail=\n"
            + "\n".join(self.stdout_lines[-lines:])
            + "\nstderr_tail=\n"
            + "\n".join(self.stderr_lines[-lines:])
        )

    def stop(self) -> None:
        _stop(
            server_process=self.server_process,
            stdout_thread=self.stdout_thread,
            stderr_thread=self.stderr_thread,
            synapse_dir=self.synapse_dir,
            database=self.database,
        )


class BaseSynapseE2ETest(aiounittest.AsyncTestCase):
    """Base class for Synapse E2E tests with shared infrastructure methods.

    The tests of one class share one homeserver (see `homeserver`), because
    booting Synapse and PostgreSQL is most of what an E2E test costs. Sharing
    means a test runs on a server that earlier tests in its class have used, so
    each test registers its own users through `new_user`, whose localparts are
    unique to the test, and asserts only on what it created itself — never on
    server-wide contents such as "the directory holds two users".
    """

    server_url: str
    database_url: str
    # Set on each concrete class the first time one of its tests asks for a
    # homeserver; read through `cls.__dict__` so a subclass never inherits
    # its parent's server.
    _shared_homeserver: Optional[Homeserver] = None

    def setUp(self) -> None:
        super().setUp()

        cls = type(self)
        cls._tests_started = cls.__dict__.get("_tests_started", 0) + 1
        # Unique among the tests of this class, so among everything on the
        # shared homeserver: suffix it onto anything that must not collide
        # with an earlier test's (localparts, aliases, email addresses).
        self.tag = f"t{cls._tests_started}"
        self._attached: Optional[Homeserver] = None
        self._log_marks = (0, 0)

        self._resource_warning_context = warnings.catch_warnings(record=True)
        self._caught_resource_warnings = self._resource_warning_context.__enter__()
        warnings.simplefilter("always", ResourceWarning)

        original_request = requests.sessions.Session.request

        def request_with_closed_connections(
            session: requests.sessions.Session,
            method: str,
            url: str,
            **kwargs: Any,
        ):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Connection", "close")
            kwargs["headers"] = headers
            return original_request(session, method, url, **kwargs)

        request_patcher = patch.object(
            requests.sessions.Session,
            "request",
            new=request_with_closed_connections,
        )
        request_patcher.start()
        self.addCleanup(request_patcher.stop)

    def tearDown(self) -> None:
        super().tearDown()

        gc.collect()
        resource_warnings = [
            warning
            for warning in self._caught_resource_warnings
            if issubclass(warning.category, ResourceWarning)
            and not _is_ignorable_resource_warning(warning)
        ]
        self._resource_warning_context.__exit__(None, None, None)

        if resource_warnings:
            warning_messages = "\n".join(
                f"{warning.filename}:{warning.lineno}: {warning.message}"
                for warning in resource_warnings
            )
            self.fail(
                "ResourceWarning emitted during E2E test:\n" f"{warning_messages}"
            )

        # A shared homeserver that died during this test is this test's
        # failure, not the next one's: report it here, with the server's last
        # words, and let the next test boot a fresh one.
        shared = type(self).__dict__.get("_shared_homeserver")
        if shared is not None and shared is self._attached and not shared.running():
            type(self)._shared_homeserver = None
            shared.stop()
            self.fail(
                "The shared homeserver exited during this test "
                f"(exit_code={shared.server_process.returncode}).\n"
                f"{shared.log_tail()}"
            )

    async def homeserver(
        self,
        *,
        module_config: Optional[Dict[str, Any]] = None,
        synapse_config_overrides: Optional[Dict[str, Any]] = None,
    ) -> Homeserver:
        """This class's shared homeserver, started on first use.

        A test asking for a different configuration from the one running
        restarts it, so a class whose tests need different configurations is
        still correct, but pays a boot at every change. Give each
        configuration its own class instead. Anything the configuration
        points at (an SMTP sink, a mock CMS) must outlive the test, so start
        it in ``setUpClass`` rather than per test; a per-test port in the
        configuration means a boot per test.
        """
        cls = type(self)
        key = _config_key(module_config, synapse_config_overrides)
        current = cls.__dict__.get("_shared_homeserver")
        if current is not None and (current.key != key or not current.running()):
            cls._shared_homeserver = None
            current.stop()
            current = None
        if current is None:
            current = await self._boot_homeserver(
                module_config, synapse_config_overrides, key
            )
            cls._shared_homeserver = current
            if not cls.__dict__.get("_shared_cleanup_registered", False):
                cls._shared_cleanup_registered = True
                cls.addClassCleanup(cls._stop_shared_homeserver)
        self._attach_homeserver(current)
        return current

    @classmethod
    def _stop_shared_homeserver(cls) -> None:
        shared = cls.__dict__.get("_shared_homeserver")
        cls._shared_homeserver = None
        if shared is not None:
            shared.stop()

    def stop_homeserver(self) -> None:
        """Stop the class's homeserver now, for a test of the shutdown itself.

        The server's output stays readable through `server_log()`; the next
        test to ask for a homeserver boots a fresh one.
        """
        type(self)._stop_shared_homeserver()

    def _attach_homeserver(self, homeserver: Homeserver) -> None:
        self._attached = homeserver
        self.server_url = homeserver.url
        # Exposed so a test can read the module's own tables back out of the
        # database Synapse is actually using. The shared PostgreSQL's own URL
        # points at its default database, not this server's, so a test that
        # reaches for it queries an empty schema and reports a missing row as
        # a missing feature.
        self.database_url = homeserver.database_url
        # The whole of the server's output, for the per-test servers of
        # `start_test_synapse`; a test on a shared server reads `server_log`.
        self.server_stdout_lines = homeserver.stdout_lines
        self.server_stderr_lines = homeserver.stderr_lines
        self._log_marks = (len(homeserver.stdout_lines), len(homeserver.stderr_lines))

    def server_log(self) -> list[str]:
        """What the homeserver has written since this test attached to it.

        On a shared server the earlier tests' output is still in the buffers,
        so a test asserting on log lines must read only its own.
        """
        if self._attached is None:
            raise AssertionError("No homeserver is attached to this test")
        stdout_mark, stderr_mark = self._log_marks
        return (
            self._attached.stdout_lines[stdout_mark:]
            + self._attached.stderr_lines[stderr_mark:]
        )

    def localpart(self, name: str) -> str:
        """The localpart `new_user(name)` registers in this test."""
        return f"{name}_{self.tag}"

    async def new_user(
        self, name: str, *, admin: bool = False, password: str = "pw"
    ) -> Tuple[str, str]:
        """Register a user unique to this test; returns (user_id, access_token).

        The localpart is `localpart(name)`, so the same name in two tests of a
        class is two users. The token is the one registration issues, so no
        login (and none of the per-address login rate limit) is spent.
        """
        return await self._register_localpart(self.localpart(name), password, admin)

    async def _register_localpart(
        self, localpart: str, password: str, admin: bool
    ) -> Tuple[str, str]:
        """Shared-secret registration, as `register_new_matrix_user` does it."""
        if self._attached is None:
            raise AssertionError("Start a homeserver before registering users")
        url = f"{self.server_url}/_synapse/admin/v1/register"
        nonce_response = requests.get(url, timeout=30)
        self.assertEqual(nonce_response.status_code, 200, nonce_response.text)
        nonce = nonce_response.json()["nonce"]
        mac = hmac.new(
            key=self._attached.registration_shared_secret.encode("utf8"),
            digestmod=hashlib.sha1,
        )
        mac.update(nonce.encode("utf8"))
        mac.update(b"\x00")
        mac.update(localpart.encode("utf8"))
        mac.update(b"\x00")
        mac.update(password.encode("utf8"))
        mac.update(b"\x00")
        mac.update(b"admin" if admin else b"notadmin")
        response = requests.post(
            url,
            json={
                "nonce": nonce,
                "username": localpart,
                "password": password,
                "mac": mac.hexdigest(),
                "admin": admin,
            },
            timeout=30,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        return body["user_id"], body["access_token"]

    async def _wait_for_background_updates(
        self, db_url: str, timeout_seconds: float = 60.0
    ) -> None:
        """Block until Synapse has drained its startup background updates.

        See the `background_updates` note in `_boot_homeserver`: until the user-directory
        rebuild has run, a test can watch its own registrations vanish from
        the directory. Synapse deletes a row from `background_updates` as each
        update completes, so an empty table is the done signal.
        """
        deadline = time.monotonic() + timeout_seconds
        conn = psycopg2.connect(db_url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                while True:
                    cur.execute("SELECT update_name FROM background_updates")
                    remaining = [row[0] for row in cur.fetchall()]
                    if not remaining:
                        return
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            "Synapse background updates still pending after "
                            f"{timeout_seconds}s: {remaining}"
                        )
                    await asyncio.sleep(0.25)
        finally:
            conn.close()

    async def start_test_synapse(
        self,
        *,
        module_config: Optional[Dict[str, Any]] = None,
        synapse_config_overrides: Optional[Dict[str, Any]] = None,
    ) -> Tuple[
        _Database,
        str,
        str,
        subprocess.Popen,
        threading.Thread,
        threading.Thread,
    ]:
        """Start a homeserver of this test's own, which the test must stop.

        For a test about the boot and teardown cycle itself; every other test
        uses the class's shared `homeserver`.

        Returns (database, synapse_dir, config_path, server_process, stdout_thread, stderr_thread);
        pass them all to `stop_synapse`.
        """
        homeserver = await self._boot_homeserver(
            module_config,
            synapse_config_overrides,
            _config_key(module_config, synapse_config_overrides),
        )
        self._attach_homeserver(homeserver)
        return (
            homeserver.database,
            homeserver.synapse_dir,
            homeserver.config_path,
            homeserver.server_process,
            homeserver.stdout_thread,
            homeserver.stderr_thread,
        )

    async def _boot_homeserver(
        self,
        module_config: Optional[Dict[str, Any]],
        synapse_config_overrides: Optional[Dict[str, Any]],
        key: str,
    ) -> Homeserver:
        """Start a test Synapse server backed by PostgreSQL, on a free port."""
        database: Optional[_Database] = None
        synapse_dir: Optional[str] = None
        server_process: Optional[subprocess.Popen] = None
        stdout_thread: Optional[threading.Thread] = None
        stderr_thread: Optional[threading.Thread] = None
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        try:
            database = _new_database()
            db_url = database.url

            synapse_dir = tempfile.mkdtemp()
            config_path = _write_generated_config(synapse_dir)

            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            log_config_path = config.get("log_config")

            effective_module_config = {
                "export_user_data_output_dir": os.path.join(
                    synapse_dir, "export-user-data"
                ),
                "cms_base_url": "http://127.0.0.1:9",
                "cms_service_api_key": "test-cms-api-key",
            }
            if module_config:
                effective_module_config.update(module_config)

            config["modules"] = [
                {
                    "module": "synapse_pangea_chat.PangeaChat",
                    "config": effective_module_config,
                }
            ]

            dsn_params = parse_dsn(db_url)
            config["database"] = {
                "name": "psycopg2",
                "args": dsn_params,
            }

            # A free port per server rather than the generated 8008, so a test
            # run never collides with a local-stack Synapse, and two runs can
            # proceed side by side. A caller that brings its own listeners
            # (to know the URL before the server starts) keeps them.
            if synapse_config_overrides and "listeners" in synapse_config_overrides:
                port = synapse_config_overrides["listeners"][0]["port"]
            else:
                port = _free_port()
                for listener in config["listeners"]:
                    listener["port"] = port
                    listener["bind_addresses"] = ["127.0.0.1"]
            url = f"http://127.0.0.1:{port}"

            workspace_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )
            template_dir = os.path.join(
                workspace_root, "synapse-templates", "templates"
            )
            test_template_dir = os.path.join(synapse_dir, "test-templates")
            os.makedirs(test_template_dir, exist_ok=True)

            if os.path.isdir(template_dir):
                for template_name in os.listdir(template_dir):
                    src = os.path.join(template_dir, template_name)
                    dst = os.path.join(test_template_dir, template_name)
                    if os.path.isfile(src):
                        shutil.copyfile(src, dst)

            invite_templates = {
                "course_invite.html": "<html><body>{{ app_name }}</body></html>",
                "course_invite.txt": "{{ app_name }}",
            }
            for template_name, template_contents in invite_templates.items():
                template_path = os.path.join(test_template_dir, template_name)
                if not os.path.exists(template_path):
                    with open(template_path, "w", encoding="utf-8") as template_file:
                        template_file.write(template_contents)

            templates_config = config.get("templates", {})
            if not isinstance(templates_config, dict):
                templates_config = {}
            templates_config["custom_template_directory"] = test_template_dir
            config["templates"] = templates_config

            # Synapse 1.126 flipped room_list_publication_rules to deny-all.
            # Deployment pins the pre-1.126 allow-all rule explicitly
            # (pangeachat/ansible#235); the test homeserver mirrors it, and
            # this doubles as the canary for course discovery, which requires
            # rooms to be publishable to the room directory.
            config["room_list_publication_rules"] = [{"action": "allow"}]

            # Synapse 1.159 added rc_room_creation (default: 1 room/min,
            # burst 10) — far below what the suite creates. Unknown key on
            # 1.124, where Synapse ignores it.
            config["rc_room_creation"] = {"per_second": 1000, "burst_count": 1000}

            # These limits are keyed by client address, and every test talks
            # from 127.0.0.1, so on a shared homeserver each test would inherit
            # the buckets earlier tests drained. A test's own override of one
            # of these keys still wins.
            unlimited = {"per_second": 9999, "burst_count": 9999}
            config["rc_login"] = {
                "address": unlimited,
                "account": unlimited,
                "failed_attempts": unlimited,
            }
            config["rc_registration"] = unlimited
            config["rc_3pid_validation"] = unlimited

            # Every registration and login hashes a password; the default 12
            # rounds cost a quarter of a second each, and nothing under test
            # depends on the hash's strength.
            config["bcrypt_rounds"] = 4

            # A fresh database boots with ~54 pending background updates, run
            # one batch per sleep interval (1 s by default). Four of them
            # rebuild the user directory, and `populate_user_directory_process_rooms`
            # starts by deleting every row of user_directory /
            # user_directory_search / users_who_share_private_rooms — about
            # 5 s after startup locally, later and wider on a slow CI runner.
            # A test registering users or sharing a room inside that window
            # searches an empty directory, and a room created after the
            # staging snapshot is never rebuilt into the shared-rooms table
            # (CI runs 35238063832 attempts 1 and 2; main's runs for #191 and
            # #207). Run the queue back-to-back so it drains before the first
            # request, and `_wait_for_background_updates` below proves it did.
            config["background_updates"] = {"sleep_enabled": False}
            if synapse_config_overrides:
                for key_name, value in synapse_config_overrides.items():
                    config[key_name] = value

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

            def read_output(pipe: Union[IO[str], None], sink: list[str]) -> None:
                if pipe is None:
                    return
                for line in iter(pipe.readline, ""):
                    sink.append(line.rstrip("\n"))
                    logger.debug(line)
                pipe.close()

            stdout_thread = threading.Thread(
                target=read_output, args=(server_process.stdout, stdout_lines)
            )
            stderr_thread = threading.Thread(
                target=read_output, args=(server_process.stderr, stderr_lines)
            )
            stdout_thread.start()
            stderr_thread.start()

            deadline = time.monotonic() + 30
            server_ready = False
            while time.monotonic() < deadline and server_process.poll() is None:
                try:
                    response = requests.get(f"{url}/health", timeout=10)
                    if response.status_code == 200:
                        server_ready = True
                        break
                except requests.exceptions.ConnectionError:
                    pass
                await asyncio.sleep(0.1)

            # A server that failed to bind its port exits; anything still
            # answering /health then is some other process.
            if not server_ready or server_process.poll() is not None:
                if server_process.poll() is None:
                    server_process.terminate()
                    try:
                        server_process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server_process.kill()
                        server_process.wait(timeout=10)

                if stdout_thread is not None:
                    stdout_thread.join(timeout=5)
                if stderr_thread is not None:
                    stderr_thread.join(timeout=5)

                stdout_tail = "\n".join(stdout_lines[-20:])
                stderr_tail = "\n".join(stderr_lines[-20:])
                raise RuntimeError(
                    "Synapse server did not start successfully. "
                    f"exit_code={server_process.returncode}. "
                    f"stdout_tail=\n{stdout_tail}\n"
                    f"stderr_tail=\n{stderr_tail}"
                )

            await self._wait_for_background_updates(db_url)
            return Homeserver(
                url=url,
                key=key,
                config_path=config_path,
                synapse_dir=synapse_dir,
                database_url=db_url,
                registration_shared_secret=config["registration_shared_secret"],
                database=database,
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
            )
        except Exception as e:
            _stop(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                database=database,
            )
            raise e

    async def register_user(
        self, config_path: str, dir: str, user: str, password: str, admin: bool
    ) -> None:
        """Register `user` exactly as named, on the homeserver this test started.

        For a test that needs a particular localpart; prefer `new_user`.
        """
        await self._register_localpart(user, password, admin)

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
        """Clean up a `start_test_synapse` server. Call in finally blocks.

        `postgres` is the database `start_test_synapse` returned first.
        """
        _stop(
            server_process=server_process,
            stdout_thread=stdout_thread,
            stderr_thread=stderr_thread,
            synapse_dir=synapse_dir,
            database=postgres,
        )

    async def create_private_room(self, access_token: str) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = f"{self.server_url}/_matrix/client/v3/createRoom"
        create_room_data = {"visibility": "private", "preset": "private_chat"}
        response = requests.post(
            create_room_url,
            json=cast(Any, create_room_data),
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
            json=cast(Any, create_room_data),
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
