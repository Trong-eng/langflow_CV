"""Process isolation and actual HTTP request boundaries."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from benchmark_analyst.runtime import Worker, read_config, run_sample


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
