"""Process isolation and actual HTTP request boundaries."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from benchmark_analyst.runtime import Worker, read_config, run_sample


@pytest.fixture
def owned_child_worker(tmp_path, monkeypatch):
    """Real POSIX launcher/server pair, without Langflow or model execution."""
    from benchmark_analyst import runtime

    monkeypatch.setattr(runtime, "STOP_GRACE_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(runtime, "STOP_KILL_SECONDS", 1.0, raising=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os,signal\nfrom http.server import BaseHTTPRequestHandler,HTTPServer\n"
        "from pathlib import Path\n"
        "if os.environ['TEST_IGNORE_TERM']=='true': signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "Path(os.environ['TEST_CHILD_PID']).write_text(str(os.getpid()))\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        " def log_message(self,*args): pass\n"
        "HTTPServer(('127.0.0.1',int(os.environ['TEST_PORT'])),Handler).serve_forever()\n"
    )
    executable = tmp_path / "bin" / "uv"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\nimport os,signal,subprocess,sys\n"
        "signal.signal(signal.SIGTERM,lambda *args: sys.exit(0))\n"
        f"child=subprocess.Popen([sys.executable,{str(child_script)!r}]); child.wait()\n"
    )
    executable.chmod(0o755)
    worker = Worker(tmp_path, port, env_file=None, credentials_file=None)
    worker.env.update(
        PATH=str(executable.parent) + os.pathsep + os.environ["PATH"],
        TEST_PORT=str(port),
        TEST_CHILD_PID=str(tmp_path / "child.pid"),
        TEST_IGNORE_TERM="true",
    )
    worker.start("00", tmp_path / "logs")
    launcher = worker.process
    child_pid = int((tmp_path / "child.pid").read_text())
    try:
        yield worker, launcher, child_pid
    finally:
        try:
            os.killpg(launcher.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        launcher.wait(timeout=5)
        if worker.scratch is not None:
            worker.scratch.cleanup()


def _process_can_run(pid):
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.parametrize("launcher_exited", [False, True])
def test_stop_terminates_term_ignoring_owned_child_before_cleanup(owned_child_worker, launcher_exited):
    worker, launcher, child_pid = owned_child_worker
    scratch_path = worker.scratch.name
    if launcher_exited:
        launcher.kill()
        launcher.wait(timeout=5)
    assert _process_can_run(child_pid)
    worker.stop()
    assert not _process_can_run(child_pid)
    assert worker.process is None and worker.scratch is None
    assert not os.path.exists(scratch_path)
    worker.stop()  # Successful stop remains idempotent.


def test_stop_retains_ownership_and_scratch_when_child_cleanup_is_incomplete(owned_child_worker, monkeypatch):
    from benchmark_analyst import runtime

    worker, launcher, child_pid = owned_child_worker
    launcher.kill()
    launcher.wait(timeout=5)
    scratch = worker.scratch
    with monkeypatch.context() as scoped:
        scoped.setattr(runtime, "STOP_GRACE_SECONDS", 0.05, raising=False)
        scoped.setattr(runtime, "STOP_KILL_SECONDS", 0.05, raising=False)
        scoped.setattr(runtime.os, "killpg", lambda *_: None)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="worker|group"):
            worker.stop()
        assert time.monotonic() - started < 3
        assert _process_can_run(child_pid)
        assert worker.process is launcher and worker.scratch is scratch
        assert os.path.isdir(scratch.name)
    worker.stop()
    assert not _process_can_run(child_pid)


def test_stop_does_not_signal_a_group_without_a_verified_owned_generation(owned_child_worker):
    worker, launcher, _ = owned_child_worker
    original_group = worker._owned_group_id
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        worker._owned_group_id = unrelated.pid  # Simulate a stale group identifier after PID reuse.
        with pytest.raises(RuntimeError, match="ownership"):
            worker.stop()
        assert unrelated.poll() is None
        assert worker.process is launcher and worker.scratch is not None
    finally:
        worker._owned_group_id = original_group
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_cached_process_birth_cannot_authorize_signaling_an_unrelated_group(owned_child_worker, monkeypatch):
    import psutil

    worker, launcher, _ = owned_child_worker
    original_group, original_generations = worker._owned_group_id, dict(worker._owned_processes)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    scratch = worker.scratch
    signals = []
    psutil.process_iter.cache_clear()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(psutil, "pids", lambda: [unrelated.pid])
            cached = next(psutil.process_iter(["pid", "create_time", "status"]))
            fresh_birth = psutil.Process(unrelated.pid).create_time()
            stale_birth = fresh_birth - 1000
            cached._create_time = stale_birth  # Model the retained psutil cache after PID reuse.
            worker._owned_group_id = unrelated.pid
            worker._owned_processes[unrelated.pid] = stale_birth
            scoped.setattr(os, "killpg", lambda group, sig: signals.append((group, sig)))
            with pytest.raises(RuntimeError):
                worker.stop()
            assert signals == []
            assert unrelated.poll() is None
            assert worker.process is launcher and worker.scratch is scratch
            assert os.path.isdir(scratch.name)
    finally:
        worker._owned_group_id, worker._owned_processes = original_group, original_generations
        unrelated.kill()
        unrelated.wait(timeout=5)
        psutil.process_iter.cache_clear()


def test_port_check_allows_own_previous_time_wait_but_rejects_live_listener():
    from benchmark_analyst.runtime import ensure_port_free

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        with pytest.raises(RuntimeError, match="busy"):
            ensure_port_free(port)
        with socket.create_connection(("127.0.0.1", port)) as client:
            connection, _ = listener.accept()
            connection.shutdown(socket.SHUT_WR)
            connection.close()
            assert client.recv(1) == b""
    ensure_port_free(port)


def test_config_resolves_paths_and_requires_four_distinct_processing_nodes(tmp_path):
    config = {
        "flow_id": "flow",
        "input_node_id": "ChatInput-1",
        "output_node_id": "ChatOutput-1",
        "scrfd_node_ids": ["pre", "infer", "draw", "save"],
        "expected_faces": 3,
        "image_path": "image.jpg",
        "model_path": "model.onnx",
        "flow_export_path": "flow.json",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert read_config(path)["image_path"] == str(tmp_path / "image.jpg")
    config["scrfd_node_ids"] = ["same"] * 4
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        read_config(path)


def test_worker_flags_are_explicit_and_api_key_not_in_worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGFLOW_API_KEY", "private-key")
    worker = Worker(tmp_path, 7860, env_file=None, credentials_file=None)
    env = worker.environment("01", tmp_path)
    assert env["LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED"] == "false"
    assert env["LANGFLOW_WARM_REGISTRY_ENABLED"] == "true"
    assert env["LANGFLOW_WARM_REGISTRY_PRELOAD_LIMIT"] == "0"
    assert "LANGFLOW_API_KEY" not in env


def test_controlled_worker_pins_actual_treatments_and_fixture_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "true")
    monkeypatch.setenv("LANGFLOW_DO_NOT_TRACK", "true")
    monkeypatch.setenv("LANGFLOW_DATABASE_URL", "sqlite:///live.db")
    slot = {"database_path": str(tmp_path / "slot.db"), "config_dir": str(tmp_path / "storage")}
    worker = Worker(
        tmp_path,
        7860,
        env_file=None,
        credentials_file=None,
        fixture=slot,
        controlled=True,
        diagnostics=True,
        telemetry_enabled=True,
    )
    env = worker.environment("11", tmp_path)
    assert env["DO_NOT_TRACK"] == env["LANGFLOW_DO_NOT_TRACK"] == "false"
    assert env["LANGFLOW_DATABASE_URL"] == "sqlite:///" + slot["database_path"]
    assert env["LANGFLOW_CONFIG_DIR"] == slot["config_dir"]
    assert env["LANGFLOW_BENCHMARK_DIAGNOSTICS_ENABLED"] == "true"
    assert env["LANGFLOW_BENCHMARK_ACCESS_LOG"] == "false"
    assert "uvicorn.access=ERROR" in env["LANGFLOW_LOG_LEVELS"]


def test_failed_flow_is_kept_and_never_retried(tmp_path):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            observed.append(self.path)
            self.send_response(201 if "/upload/" in self.path else 500)
            self.end_headers()
            self.wfile.write(b'{"file_path":"flow/input.jpg"}' if "/upload/" in self.path else b"error")

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import requests

        config = {
            "flow_id": "flow",
            "input_node_id": "ChatInput-1",
            "output_node_id": "ChatOutput-1",
            "scrfd_node_ids": ["pre", "infer", "draw", "save"],
            "expected_faces": 3,
        }
        with requests.Session() as session:
            row = run_sample(
                session,
                f"http://127.0.0.1:{server.server_port}",
                config,
                b"image",
                "image.jpg",
                "control",
                block=1,
                arm="00",
                phase="measured",
                index=0,
                session_id="session",
            )
        assert row["outcome"] == "error"
        assert row["output_valid"] is False
        assert row["langflow_overhead_ms"] is None
        assert observed == ["/api/v1/files/upload/flow", "/api/v1/run/flow?stream=false"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_idle_checkpoint_uses_fresh_connection_without_retrying_expired_pool(tmp_path, monkeypatch):
    import requests

    peers = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_GET(self):
            peers.append(self.client_address)
            if getattr(self, "already_used", False):
                # Model the keep-alive expiry race after checkout, before response.
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            self.already_used = True
            content = b'{"pid":123,"events":[],"metadata":{}}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LANGFLOW_API_KEY", "test-key")
    worker = Worker(tmp_path, server.server_port, env_file=None, credentials_file=None)
    try:
        with worker.session() as measured_session:
            worker.snapshot(measured_session)
            assert worker.snapshot(measured_session, fresh_connection=True)["pid"] == 123
            assert worker.drain_diagnostics(measured_session, fresh_connection=True)["events"] == []
            assert len(peers) == 3 and len(set(peers)) == 3
            with pytest.raises(requests.ConnectionError):
                worker.snapshot(measured_session)
            assert len(peers) == 4 and peers[-1] == peers[0]  # No retry hides the stale socket.
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
