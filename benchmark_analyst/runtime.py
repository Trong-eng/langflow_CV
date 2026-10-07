"""One local production Langflow worker and real upload/run/image requests."""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import requests
from dotenv import dotenv_values

from benchmark_analyst.protocol import measure_overhead, pixel_digest, result_image_path, run_payload, utc_now

STOP_GRACE_SECONDS = 40
STOP_KILL_SECONDS = 10


def read_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("flow_id", "input_node_id", "output_node_id"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"{key} is required")
    nodes = config.get("scrfd_node_ids", [])
    if len(nodes) != 4 or len(set(nodes)) != 4 or any(not isinstance(n, str) or not n for n in nodes):
        raise ValueError("scrfd_node_ids must name four distinct processing nodes")
    if not isinstance(config.get("expected_faces"), int) or config["expected_faces"] < 0:
        raise ValueError("expected_faces must be a nonnegative integer")
    for key in ("image_path", "model_path", "flow_export_path"):
        value = Path(config[key]).expanduser()
        config[key] = str((path.parent / value).resolve() if not value.is_absolute() else value.resolve())
    return config


def ensure_port_free(port: int) -> None:
    with socket.socket() as probe:
        # A stopped worker can leave accepted sockets in TIME_WAIT. Match
        # uvicorn's reuse policy while still refusing a live listening socket.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"port {port} is busy; choose a free --port") from exc


class Worker:
    """Own only the process started here; never kill an existing server on a port."""

    def __init__(
        self,
        root: Path,
        port: int,
        *,
        env_file: Path | None,
        credentials_file: Path | None,
        fixture: dict | None = None,
        controlled: bool = False,
        diagnostics: bool = False,
        telemetry_enabled: bool = True,
    ):
        self.root, self.port = root, port
        values = dotenv_values(env_file) if env_file else {}
        credentials = dotenv_values(credentials_file) if credentials_file else {}
        self.env = {**{k: v for k, v in values.items() if v is not None}, **os.environ}
        self.api_key = credentials.get("LANGFLOW_API_KEY") or self.env.get("LANGFLOW_API_KEY", "")
        self.token = secrets.token_urlsafe(32)
        self.process = None
        self.scratch = None
        self._owned_group_id: int | None = None
        self._owned_processes: dict[int, float] = {}
        self.base_url = f"http://127.0.0.1:{port}"
        self.fixture, self.controlled = fixture, controlled
        self.diagnostics, self.telemetry_enabled = diagnostics, telemetry_enabled

    def environment(self, arm: str, scratch: Path) -> dict[str, str]:
        if arm not in ("00", "01", "10", "11"):
            raise ValueError("unknown arm")
        env = self.env.copy()
        env.pop("LANGFLOW_API_KEY", None)
        # Legacy HTTP reuse is no longer part of this experiment or runtime code.
        for key in list(env):
            if key.startswith(("LANGFLOW_BENCHMARK_", "LANGFLOW_HTTP_CONNECTION_REUSE_")):
                env.pop(key)
        paths = [self.root, self.root / "src/backend/base", self.root / "src/lfx/src", self.root / "src/sdk/src"]
        env["PYTHONPATH"] = os.pathsep.join(map(str, paths))
        env.update(
            {
                "LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED": str(arm[0] == "1").lower(),
                "LANGFLOW_WARM_REGISTRY_ENABLED": str(arm[1] == "1").lower(),
                "LANGFLOW_WARM_REGISTRY_PRELOAD_LIMIT": "0",
                "LANGFLOW_BENCHMARK_WORKER_IDENTITY_ENABLED": "true",
                "LANGFLOW_BENCHMARK_CONTROL_ENABLED": "true",
                "LANGFLOW_BENCHMARK_CONTROL_TOKEN": self.token,
                "LANGFLOW_BENCHMARK_WORKER_COUNT": "1",
                "LANGFLOW_WORKERS": "1",
                "LANGFLOW_NATIVE_TRACING": "true",
                "LANGFLOW_DEACTIVATE_TRACING": "false",
                "TMPDIR": str(scratch),
                "TMP": str(scratch),
                "TEMP": str(scratch),
            }
        )
        env["LANGFLOW_BENCHMARK_DIAGNOSTICS_ENABLED"] = str(self.diagnostics).lower()
        env["LANGFLOW_BENCHMARK_DIAGNOSTIC_DETAIL"] = "coarse"
        if self.controlled:
            env["LANGFLOW_BENCHMARK_ACCESS_LOG"] = "false"
            env["DO_NOT_TRACK"] = env["LANGFLOW_DO_NOT_TRACK"] = str(not self.telemetry_enabled).lower()
            levels = [
                value
                for value in env.get("LANGFLOW_LOG_LEVELS", "").split(",")
                if value and not value.startswith("uvicorn.access=")
            ]
            env["LANGFLOW_LOG_LEVELS"] = ",".join([*levels, "uvicorn.access=ERROR"])
        if self.fixture is not None:
            env["LANGFLOW_DATABASE_URL"] = "sqlite:///" + self.fixture["database_path"]
            env["LANGFLOW_CONFIG_DIR"] = self.fixture["config_dir"]
            env["LANGFLOW_SAVE_DB_IN_CONFIG_DIR"] = "true"
            env["LANGFLOW_STORAGE_TYPE"] = "local"
        return env

    def start(self, arm: str, directory: Path) -> None:
        import psutil

        if self.process is not None:
            raise RuntimeError("worker already owned; stop it before starting another")
        ensure_port_free(self.port)
        directory.mkdir(parents=True, exist_ok=True)
        self.scratch = tempfile.TemporaryDirectory(prefix="worker-tmp-", dir=directory)
        args = [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "uvicorn",
            "--factory",
            "langflow.benchmark_worker_identity:create_benchmark_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--workers",
            "1",
            "--loop",
            "asyncio",
            "--no-access-log",
        ]
        with (directory / "worker.log").open("w", encoding="utf-8") as log:
            self.process = subprocess.Popen(
                args,
                cwd=self.root,
                env=self.environment(arm, Path(self.scratch.name)),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            self._owned_group_id = self.process.pid
            self._owned_processes = {self.process.pid: psutil.Process(self.process.pid).create_time()}
            deadline = time.monotonic() + 180
            with requests.Session() as session:
                session.trust_env = False
                while time.monotonic() < deadline:
                    self._remember_owned_descendants()
                    if self.process.poll() is not None:
                        raise RuntimeError(f"worker exited; inspect {directory / 'worker.log'}")
                    try:
                        if session.get(self.base_url + "/health_check", timeout=2).status_code == 200:
                            self._remember_owned_descendants()
                            return
                    except requests.RequestException:
                        pass
                    time.sleep(0.25)
            raise RuntimeError("worker readiness timeout")
        except BaseException:
            self.stop()
            raise

    def _remember_owned_descendants(self) -> None:
        """Capture child generations while their relationship is still provable."""
        import psutil

        if self.process is None or self._owned_group_id is None:
            return
        try:
            parent = psutil.Process(self.process.pid)
            if parent.create_time() != self._owned_processes.get(parent.pid):
                return
            for child in parent.children(recursive=True):
                try:
                    if os.getpgid(child.pid) == self._owned_group_id:
                        self._owned_processes[child.pid] = child.create_time()
                except (OSError, psutil.Error):
                    continue
        except psutil.Error:
            return

    def _live_group_members(self) -> dict[int, float | None]:
        import psutil

        members = {}
        for pid in psutil.pids():
            try:
                if os.getpgid(pid) != self._owned_group_id:
                    continue
                # process_iter retains Process objects and their cached birth time.
                # Ownership needs the current generation, including after PID reuse.
                process = psutil.Process(pid)
                if process.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    members[pid] = process.create_time()
            except (OSError, psutil.Error):
                continue
        return members

    def _signal_owned_group(self, sig: int) -> None:
        members = self._live_group_members()
        if not members:
            return
        if not any(
            created is not None and self._owned_processes.get(pid) == created for pid, created in members.items()
        ):
            raise RuntimeError("worker group ownership cannot be verified; retained worker and scratch")
        try:
            os.killpg(self._owned_group_id, sig)
        except ProcessLookupError:
            pass  # The verified group exited before the signal.

    def _wait_for_group_stop(self, timeout: float) -> bool:
        import psutil

        def known_process_running() -> bool:
            for pid, created in self._owned_processes.items():
                try:
                    process = psutil.Process(pid)
                    if process.create_time() == created and process.status() not in (
                        psutil.STATUS_ZOMBIE,
                        psutil.STATUS_DEAD,
                    ):
                        return True
                except psutil.NoSuchProcess:
                    continue
                except psutil.Error:
                    return True  # An unreadable owned generation is not proof of exit.
            return False

        deadline = time.monotonic() + timeout
        while self._live_group_members() or known_process_running():
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        return True

    def stop(self) -> None:
        if self.process is not None:
            if self._owned_group_id is None:
                raise RuntimeError("worker group ownership is missing; retained worker and scratch")
            self._remember_owned_descendants()
            self._signal_owned_group(signal.SIGTERM)
            if not self._wait_for_group_stop(STOP_GRACE_SECONDS):
                self._signal_owned_group(signal.SIGKILL)
                if not self._wait_for_group_stop(STOP_KILL_SECONDS):
                    raise RuntimeError("worker group still running after stop deadline; retained worker and scratch")
            self.process.wait(timeout=STOP_KILL_SECONDS)
            self.process = None
            self._owned_group_id = None
            self._owned_processes = {}
        if self.scratch is not None:
            self.scratch.cleanup()
            self.scratch = None

    def session(self) -> requests.Session:
        if not self.api_key:
            raise ValueError("LANGFLOW_API_KEY is missing; use --credentials-file or environment")
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"x-api-key": self.api_key, "accept": "application/json"})
        return session

    def snapshot(self, session: requests.Session, *, fresh_connection: bool = False) -> dict:
        if fresh_connection:
            # The 5 s idle checkpoint coincides with uvicorn's keep-alive expiry.
            # Use one fresh control connection; never retry a measured request.
            with self.session() as control:
                return self.snapshot(control)
        started_at, started = utc_now(), time.perf_counter()
        response = session.get(
            self.base_url + "/_benchmark/snapshot",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
            allow_redirects=False,
        )
        response.raise_for_status()
        return {
            **response.json(),
            "snapshot_started_at_utc": started_at,
            "snapshot_duration_ms": (time.perf_counter() - started) * 1000,
        }

    def drain_diagnostics(self, session: requests.Session, *, fresh_connection: bool = False) -> dict:
        if fresh_connection:
            with self.session() as control:
                return self.drain_diagnostics(control)
        response = session.get(
            self.base_url + "/_benchmark/diagnostics/drain",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=15,
            allow_redirects=False,
        )
        response.raise_for_status()
        return response.json()


def run_sample(
    session: requests.Session,
    base_url: str,
    config: dict,
    image: bytes,
    filename: str,
    control_token: str,
    *,
    block: int,
    arm: str,
    phase: str,
    index: int,
    session_id: str,
    reference_path: Path | None = None,
) -> dict:
    request_id = str(uuid4())
    flow_id = quote(config["flow_id"], safe="")
    timeout = float(config.get("timeout_seconds", 60))
    row = dict(
        block=block,
        arm=arm,
        phase=phase,
        index=index,
        request_id=request_id,
        pid=None,
        outcome="error",
        error=None,
        output_valid=False,
        langflow_overhead_ms=None,
        server_total_ms=None,
        scrfd_processing_ms=None,
        upload_ms=None,
        flow_api_ms=None,
        component_intervals_ms=[],
        warm_path=None,
        started_at=utc_now(),
    )
    measurement = None
    try:
        started = time.perf_counter()
        response = session.post(
            base_url + f"/api/v1/files/upload/{flow_id}",
            files={"file": (f"{request_id}-{filename}", image, mimetypes.guess_type(filename)[0] or "image/jpeg")},
            timeout=timeout,
            allow_redirects=False,
        )
        row["upload_ms"] = (time.perf_counter() - started) * 1000
        if response.status_code not in (200, 201):
            raise RuntimeError(f"upload HTTP {response.status_code}")
        file_path = response.json()["file_path"]
        row["uploaded_file_path"] = file_path
        started = time.perf_counter()
        response = session.post(
            base_url + f"/api/v1/run/{flow_id}?stream=false",
            json=run_payload(config, file_path, request_id, session_id),
            headers={"X-Langflow-Benchmark-Request-ID": request_id},
            timeout=timeout,
            allow_redirects=False,
        )
        row["flow_api_ms"] = (time.perf_counter() - started) * 1000
        row["http_status"] = response.status_code
        if response.status_code != 200:
            raise RuntimeError(f"run HTTP {response.status_code}")
        if response.headers.get("X-Langflow-Benchmark-Request-ID") != request_id:
            raise ValueError("worker request ID was not echoed")
        row["pid"] = int(response.headers["X-Langflow-Worker-PID"])
        measurement_response = session.get(
            base_url + f"/_benchmark/measurement/{request_id}",
            headers={"Authorization": f"Bearer {control_token}"},
            timeout=10,
            allow_redirects=False,
        )
        measurement_response.raise_for_status()
        measurement = measurement_response.json()
        if measurement["request_id"] != request_id or measurement["pid"] != row["pid"]:
            raise ValueError("measurement identity mismatch")
        row["component_intervals_ms"] = measurement["component_intervals_ms"]
        row["warm_path"] = measurement.get("warm_path")
        row.update(measure_overhead(measurement, config["scrfd_node_ids"]))
        image_path = result_image_path(
            response.json(), config["output_node_id"], config["flow_id"], config["expected_faces"]
        )
        row["output_image_path"] = image_path
        # Output download/decoding verify the real result OUTSIDE the server timing boundary.
        image_response = session.get(base_url + image_path, timeout=timeout, allow_redirects=False)
        if image_response.status_code != 200:
            raise ValueError(f"output image HTTP {image_response.status_code}")
        digest = pixel_digest(image_response.content)
        row["image_pixel_sha256"] = digest
        expected = config.get("reference_pixel_sha256")
        if expected and digest != expected:
            raise ValueError("result pixels differ from the frozen reference")
        if reference_path is not None:
            reference_path.write_bytes(image_response.content)
        row.update(outcome="success", output_valid=True)
    except requests.Timeout:
        row.update(outcome="timeout", error="HTTP request timed out")
    except (ValueError, KeyError, TypeError) as exc:
        row.update(outcome="invalid", error=f"{type(exc).__name__}: {exc}")
    except (requests.RequestException, RuntimeError) as exc:
        row.update(outcome="error", error=str(exc))
    finally:
        row["finished_at"] = utc_now()
    return row


def cleanup_inputs(session: requests.Session, base_url: str, flow_id: str, paths: list[str]) -> list[dict]:
    outcomes = []
    for path in paths:
        name = quote(path.rstrip("/").split("/")[-1], safe="")
        try:
            response = session.delete(
                f"{base_url}/api/v1/files/delete/{quote(flow_id, safe='')}/{name}", timeout=15, allow_redirects=False
            )
            outcomes.append({"file_name": name, "ok": response.status_code < 400, "status": response.status_code})
        except requests.RequestException as exc:
            outcomes.append({"file_name": name, "ok": False, "error": type(exc).__name__})
    return outcomes
