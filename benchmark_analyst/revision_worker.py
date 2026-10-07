"""Identical external instrumentation for original MAIN and compilation-only source.

The factory is outside both application checkouts. MAIN never imports the cache
implementation; its original compiler is observed without changing its behavior.
"""

from __future__ import annotations

import gc
import hmac
import importlib
import ipaddress
import logging
import os
import platform
import subprocess
import sys
import tempfile
import time
from importlib.metadata import distributions
from pathlib import Path
from uuid import UUID

import requests
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from benchmark_analyst.protocol import file_digest
from benchmark_analyst.runtime import Worker, ensure_port_free
from benchmark_analyst.worker_observer import MAINTENANCE_SETTINGS, WorkerObserver, _serving_database_provenance

HARNESS_ROOT = Path(__file__).resolve().parents[1]
IDENTITY_MODULES = (
    "langflow",
    "langflow.main",
    "langflow.api.v1.endpoints",
    "lfx",
    "lfx.custom.custom_component.component",
    "lfx.custom.utils",
    "lfx.custom.validate",
    "lfx.custom.eval",
)


class RevisionWorker(Worker):
    """Use one explicit interpreter, selected application source and shared observer."""

    def __init__(self, *args, group: str, **kwargs):
        super().__init__(*args, **kwargs)
        if group not in ("MAIN", "COMPILE"):
            raise ValueError("unknown revision group")
        self.group = group

    def environment(self, arm: str, scratch: Path) -> dict[str, str]:
        if arm != self.group:
            raise ValueError("worker group differs from requested slot")
        env = super().environment("10" if arm == "COMPILE" else "00", scratch)
        paths = [HARNESS_ROOT, self.root / "src/backend/base", self.root / "src/lfx/src", self.root / "src/sdk/src"]
        env["PYTHONPATH"] = os.pathsep.join(map(str, paths))
        env["LANGFLOW_BENCHMARK_REVISION_GROUP"] = self.group
        env["LANGFLOW_BENCHMARK_REVISION_ROOT"] = str(self.root.resolve())
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def start(self, arm: str, directory: Path) -> None:
        import psutil

        if self.process is not None:
            raise RuntimeError("worker already owned")
        ensure_port_free(self.port)
        directory.mkdir(parents=True, exist_ok=False)
        self.scratch = tempfile.TemporaryDirectory(prefix="worker-tmp-", dir=directory)
        args = [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "benchmark_analyst.revision_worker:create_app",
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
        with (directory / "worker.log").open("x", encoding="utf-8") as log:
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


def dependency_versions() -> dict[str, str]:
    """Same installed environment for both groups; names and versions only."""
    return dict(sorted((d.metadata["Name"].lower(), d.version) for d in distributions() if d.metadata["Name"]))


def optional_cache_accounting(cache):
    account = getattr(cache, "component_compilation_cache_accounting", None)
    return account() if account else None


def module_origins(root: Path, modules: dict | None = None) -> tuple[dict, list]:
    """Inspect already-loaded modules without materializing compatibility aliases."""
    origins, failures = {}, []
    package_roots = (root / "src/backend/base/langflow", root / "src/lfx/src/lfx", root / "src/sdk/src/langflow_sdk")
    for name, module in tuple((sys.modules if modules is None else modules).items()):
        if name not in {"langflow", "lfx", "langflow_sdk"} and not name.startswith(
            ("langflow.", "lfx.", "langflow_sdk.")
        ):
            continue
        if module is None:
            continue
        origin = module.__dict__.get("__file__")
        if not origin:
            continue
        origin = Path(origin).resolve()
        origins[name] = str(origin)
        if not any(origin.is_relative_to(package_root) for package_root in package_roots):
            failures.append({"module": name, "path": str(origin), "reason": "outside_selected_source"})
    return origins, failures


class RevisionObserver(WorkerObserver):
    """Reuse the symmetric dispatcher/warm-path hook; override feature-only probes."""

    def __init__(self):
        super().__init__()
        self._versions = dependency_versions()

    def snapshot(self) -> dict:
        import hashlib
        import json

        import psutil
        from langflow.services.warm_registry.service import get_warm_registry
        from lfx.services.deps import get_settings_service

        settings = get_settings_service().settings
        group = os.environ["LANGFLOW_BENCHMARK_REVISION_GROUP"]
        modules = {}
        for name in IDENTITY_MODULES:
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve()
            modules[name] = {"path": str(path), "sha256": file_digest(path)}
        compilation, accounting = None, None
        compilation_status = {"status": "unavailable", "reason": "absent_in_baseline"}
        if group == "COMPILE":
            cache = importlib.import_module("lfx.custom.component_compilation_cache")
            path = Path(cache.__file__).resolve()
            modules[cache.__name__] = {"path": str(path), "sha256": file_digest(path)}
            compilation = cache.component_compilation_cache_stats()
            accounting = optional_cache_accounting(cache)
            compilation_status = {"status": "measured", "reason": None}
        registry = get_warm_registry()
        access = logging.getLogger("uvicorn.access")
        config_dir = getattr(settings, "config_dir", None)
        versions = {"python": platform.python_version(), **self._versions}
        root = Path(os.environ["LANGFLOW_BENCHMARK_REVISION_ROOT"]).resolve()
        origins, failures = module_origins(root)
        return {
            "pid": os.getpid(),
            "process_create_time": psutil.Process().create_time(),
            "worker_count": 1,
            "settings": {
                "component_compilation_cache_enabled": getattr(settings, "component_compilation_cache_enabled", None),
                "warm_registry_enabled": bool(getattr(settings, "warm_registry_enabled", False)),
            },
            "configured": {"compilation_cache": group == "COMPILE", "warm_registry": False},
            "compilation": compilation,
            "compilation_status": compilation_status,
            "warm": dict(self.warm_counters),
            "registry_entries": len(registry),
            "cache_accounting": {
                "compilation": accounting,
                "compilation_status": compilation_status,
                "compilation_accounting_status": {
                    "status": "measured" if accounting else "unavailable",
                    "reason": None if accounting else "no_accounting_interface",
                },
                "warm": {"entries": len(registry), "heap_bytes": None, "heap_status": "unavailable"},
            },
            "identity": {
                "group": group,
                "root": os.environ["LANGFLOW_BENCHMARK_REVISION_ROOT"],
                "interpreter": sys.executable,
                "modules": modules,
                "module_origins": origins,
                "module_origin_failures": failures,
                "dependency_versions": versions,
                "dependency_sha256": hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest(),
            },
            "effective": {
                "config_dir": os.path.realpath(config_dir) if config_dir else None,
                "storage_type": getattr(settings, "storage_type", None),
                "product_telemetry_enabled": not bool(getattr(settings, "do_not_track", False)),
                "native_tracing": not bool(getattr(settings, "deactivate_tracing", False))
                and os.environ.get("LANGFLOW_NATIVE_TRACING", "true").lower() not in {"false", "0", "no"},
                "telemetry_writer_enabled": bool(getattr(settings, "telemetry_writer_enabled", False)),
                "maintenance_settings": {name: getattr(settings, name, None) for name in MAINTENANCE_SETTINGS},
                "uvicorn_access_enabled": access.isEnabledFor(logging.INFO),
                "uvicorn_access_level": logging.getLevelName(access.getEffectiveLevel()),
                "uvicorn_access_handlers": [type(h).__name__ for h in access.handlers],
                "uvicorn_access_propagate": access.propagate,
                "gc_enabled": gc.isenabled(),
                "gc_thresholds": list(gc.get_threshold()),
                "thread_environment": {
                    name: os.environ.get(name)
                    for name in (
                        "OMP_NUM_THREADS",
                        "MKL_NUM_THREADS",
                        "OPENBLAS_NUM_THREADS",
                        "NUMEXPR_NUM_THREADS",
                        "VECLIB_MAXIMUM_THREADS",
                    )
                },
                "exporters_configured": {
                    name: bool(os.environ.get(name))
                    for name in (
                        "LANGCHAIN_API_KEY",
                        "LANGFUSE_PUBLIC_KEY",
                        "LANGWATCH_API_KEY",
                        "PHOENIX_COLLECTOR_ENDPOINT",
                        "OTEL_EXPORTER_OTLP_ENDPOINT",
                    )
                },
                "versions": versions,
            },
        }

    async def snapshot_async(self) -> dict:
        snapshot = self.snapshot()
        snapshot["effective"].update(await _serving_database_provenance())
        return snapshot


class RevisionMiddleware:
    """Clock starts at outer ASGI entry and ends after the final response body send."""

    def __init__(self, app, *, observer=None):
        self.app, self.observer = app, observer or RevisionObserver()
        self._active_request = False

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":

            async def lifespan_send(message):
                if message["type"] == "lifespan.startup.complete":
                    self.observer.apply_access_logging_override()
                await send(message)

            with self.observer.instrumentation():
                return await self.app(scope, receive, lifespan_send)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = self.observer.clock()
        path = scope.get("path", "")
        if path.startswith("/_benchmark/"):
            return await self._control(scope, receive, send)
        request_id = Headers(scope=scope).get("x-langflow-benchmark-request-id")
        if not request_id or not path.startswith("/api/v1/run/"):
            return await self.app(scope, receive, send)
        try:
            UUID(request_id)
        except ValueError:
            return await JSONResponse({"error": "benchmark request ID must be a UUID"}, status_code=400)(
                scope, receive, send
            )
        if os.environ.get("LANGFLOW_BENCHMARK_WORKER_COUNT") != "1" or self._active_request:
            return await JSONResponse({"error": "one sequential worker required"}, status_code=409)(
                scope, receive, send
            )
        self._active_request = True
        record, status = self.observer.begin(request_id, start_ns=started), None

        async def observed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": list(message.get("headers", []))}
                headers = MutableHeaders(scope=message)
                headers["X-Langflow-Benchmark-Request-ID"] = request_id
                headers["X-Langflow-Worker-PID"] = str(os.getpid())
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self.observer.finish(record, status_code=status, complete=True)

        try:
            with self.observer.instrumentation(), self.observer.bind(record):
                return await self.app(scope, receive, observed_send)
        finally:
            self.observer.finish(record, status_code=status, complete=False)
            self._active_request = False

    async def _control(self, scope, receive, send):
        peer = (scope.get("client") or ("", 0))[0]
        try:
            loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        token = os.environ.get("LANGFLOW_BENCHMARK_CONTROL_TOKEN", "")
        authorization = Headers(scope=scope).get("authorization", "")
        if not loopback or not token or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            response = JSONResponse({"error": "control authentication failed"}, status_code=403)
        elif scope["method"] != "GET":
            response = JSONResponse({"error": "GET required"}, status_code=405)
        elif scope["path"] == "/_benchmark/snapshot":
            response = JSONResponse(await self.observer.snapshot_async())
        elif scope["path"].startswith("/_benchmark/measurement/"):
            request_id = scope["path"].removeprefix("/_benchmark/measurement/")
            try:
                UUID(request_id)
                result = self.observer.consume(request_id)
            except ValueError:
                result = None
            response = JSONResponse(
                result if result else {"error": "measurement not found"}, status_code=200 if result else 404
            )
        else:
            response = JSONResponse({"error": "unknown benchmark endpoint"}, status_code=404)
        return await response(scope, receive, send)


def create_app():
    from langflow.main import create_app as production_app

    return RevisionMiddleware(production_app())
