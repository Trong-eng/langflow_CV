"""Explicitly opt-in ASGI benchmark timing and loopback-only worker evidence.

The benchmark factory wraps the complete production ASGI application, including
its middleware and telemetry. There is no file/path control API. Production
response payloads are not modified.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from uuid import UUID

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse


def _enabled() -> bool:
    return (
        os.environ.get("LANGFLOW_BENCHMARK_WORKER_IDENTITY_ENABLED", "").lower() == "true"
        and os.environ.get("LANGFLOW_BENCHMARK_CONTROL_ENABLED", "").lower() == "true"
    )


def create_benchmark_app():
    """Wrap the production app outside its complete middleware and telemetry stack.

    This factory is used only by the benchmark runner. Registering the observer
    with FastAPI.add_middleware would leave later middleware and instrumentation
    outside the clock; wrapping the app also preserves the normal app factory.
    """
    from langflow.main import create_app

    return BenchmarkWorkerIdentityMiddleware(create_app())


class BenchmarkWorkerIdentityMiddleware:
    """Observe a sequential benchmark at entry through the final ASGI body send."""

    def __init__(self, app, *, observer=None):
        self.app = app
        self._observer = observer
        self._active_request = False

    @property
    def observer(self):
        if self._observer is None:
            from benchmark_analyst.worker_observer import WorkerObserver

            self._observer = WorkerObserver()
        return self._observer

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan" and _enabled():
            with self.observer.instrumentation():
                return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Capture at the observation boundary before header validation. This
        # middleware is intentionally outside response serialization.
        started = self.observer.clock() if _enabled() else None
        path = scope.get("path", "")
        if path.startswith("/_benchmark/"):
            return await self._control(scope, receive, send)
        headers = Headers(scope=scope)
        request_id = headers.get("x-langflow-benchmark-request-id")
        if not _enabled() or not request_id or not path.startswith("/api/v1/run/"):
            return await self.app(scope, receive, send)
        try:
            UUID(request_id)
        except ValueError:
            return await JSONResponse({"error": "benchmark request ID must be a UUID"}, status_code=400)(
                scope,
                receive,
                send,
            )
        if os.environ.get("LANGFLOW_BENCHMARK_WORKER_COUNT") != "1" or self._active_request:
            return await JSONResponse(
                {"error": "benchmark requires one worker and sequential requests"}, status_code=409
            )(scope, receive, send)
        self._active_request = True
        record = self.observer.begin(request_id, start_ns=started)
        status_code = None

        async def observed_send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message = {**message, "headers": list(message.get("headers", []))}
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Langflow-Benchmark-Request-ID"] = request_id
                response_headers["X-Langflow-Worker-PID"] = str(os.getpid())
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self.observer.finish(record, status_code=status_code, complete=True)

        try:
            with self.observer.instrumentation(), self.observer.bind(record):
                return await self.app(scope, receive, observed_send)
        finally:
            self.observer.finish(record, status_code=status_code, complete=False)
            self._active_request = False

    async def _control(self, scope, receive, send):
        if not _enabled():
            response = JSONResponse({"error": "benchmark control is disabled"}, status_code=404)
        else:
            response = self._authorized_control_response(scope)
        return await response(scope, receive, send)

    def _authorized_control_response(self, scope):
        peer = (scope.get("client") or ("", 0))[0]
        try:
            loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            return JSONResponse({"error": "benchmark control is loopback-only"}, status_code=403)
        token = os.environ.get("LANGFLOW_BENCHMARK_CONTROL_TOKEN", "")
        authorization = Headers(scope=scope).get("authorization", "")
        if not token or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            return JSONResponse({"error": "benchmark control authentication failed"}, status_code=403)
        if scope["method"] != "GET":
            return JSONResponse({"error": "GET required"}, status_code=405, headers={"Allow": "GET"})
        if os.environ.get("LANGFLOW_BENCHMARK_WORKER_COUNT") != "1":
            return JSONResponse({"error": "benchmark requires one worker"}, status_code=409)
        path = scope["path"]
        if path == "/_benchmark/snapshot":
            result = self.observer.snapshot()
        elif path.startswith("/_benchmark/measurement/"):
            request_id = path.removeprefix("/_benchmark/measurement/")
            try:
                UUID(request_id)
            except ValueError:
                return JSONResponse({"error": "invalid measurement ID"}, status_code=400)
            result = self.observer.consume(request_id)
            if result is None:
                return JSONResponse({"error": "measurement not found"}, status_code=404)
        else:
            return JSONResponse({"error": "unknown benchmark endpoint"}, status_code=404)
        return JSONResponse(result, headers={"X-Langflow-Worker-PID": str(os.getpid())})
