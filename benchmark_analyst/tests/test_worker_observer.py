"""Behavior tests for opt-in serving-process timing and evidence."""

# Fixtures exercise private execution boundaries and use synthetic credentials.
# ruff: noqa: ARG001, INP001, PLR2004, S101, S106, SLF001, TC002

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_union_excludes_overlap_only_once():
    from benchmark_analyst.worker_observer import interval_union_ms

    assert interval_union_ms([(3, 7), (1, 5), (9, 12), (10, 11)]) == 9
    assert interval_union_ms([]) == 0


def test_record_store_consumes_once_and_evicts_oldest():
    from benchmark_analyst.worker_observer import WorkerObserver

    observer = WorkerObserver(max_records=2)
    for request_id in ("first", "second", "third"):
        record = observer.begin(request_id)
        observer.finish(record, status_code=200, complete=True)
    assert observer.consume("first") is None
    assert observer.consume("second")["complete"] is True
    assert observer.consume("second") is None
    assert observer.consume("third")["pid"] == os.getpid()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("LANGFLOW_BENCHMARK_WORKER_IDENTITY_ENABLED", "true")
    monkeypatch.setenv("LANGFLOW_BENCHMARK_CONTROL_ENABLED", "true")
    monkeypatch.setenv("LANGFLOW_BENCHMARK_WORKER_COUNT", "1")
    monkeypatch.setenv("LANGFLOW_BENCHMARK_CONTROL_TOKEN", "test-token")


async def invoke(app, path, *, headers=None, peer="127.0.0.1", method="GET", send_hook=None):
    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "scheme": "http",
        "server": ("localhost", 7860),
        "client": (peer, 12345),
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)
        if send_hook:
            send_hook(message)

    await app(scope, receive, send)
    return messages


async def test_asgi_timer_includes_serialization_and_final_send_but_not_background_work(enabled):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    ticks = [1_000_000_000]
    observer = WorkerObserver(clock=lambda: ticks[0])

    async def downstream(scope, receive, send):
        ticks[0] += 2_000_000
        await send({"type": "http.response.start", "status": 200, "headers": []})
        ticks[0] += 3_000_000  # serialization after headers / an intermediate body
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        ticks[0] += 4_000_000
        await send({"type": "http.response.body", "body": b"last", "more_body": False})
        ticks[0] += 500_000_000  # work outside the response boundary

    def send_hook(message):
        if message["type"] == "http.response.body" and not message.get("more_body"):
            ticks[0] += 1_000_000

    app = BenchmarkWorkerIdentityMiddleware(downstream, observer=observer)
    request_id = str(uuid4())
    messages = await invoke(
        app,
        "/api/v1/run/flow",
        method="POST",
        headers={
            "X-Langflow-Benchmark-Request-ID": request_id,
        },
        send_hook=send_hook,
    )
    assert observer.consume(request_id)["server_total_ms"] == 10
    assert messages[-1]["body"] == b"last"
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-langflow-benchmark-request-id"] == request_id.encode()
    assert response_headers[b"x-langflow-worker-pid"] == str(os.getpid()).encode()


async def test_worker_factory_measures_real_app_middleware_and_outer_response_send(enabled, monkeypatch):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow import benchmark_worker_identity, main
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    ticks = [1_000_000_000]
    observer = WorkerObserver(clock=lambda: ticks[0])
    execution_client = main.execution_client

    @contextlib.contextmanager
    def client_with_work(value):
        ticks[0] += 7_000_000  # Work in the actual bind_execution_client middleware.
        with execution_client(value):
            yield

    monkeypatch.setattr(main, "execution_client", client_with_work)
    monkeypatch.setattr(main, "configure", lambda: None)
    app = benchmark_worker_identity.create_benchmark_app()
    app._observer = observer

    async def endpoint(request):
        ticks[0] += 11_000_000
        return JSONResponse({"ok": True})

    app.app.router.routes.insert(0, Route("/api/v1/run/benchmark-boundary", endpoint, methods=["POST"]))

    def final_transport_send(message):
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            ticks[0] += 3_000_000

    request_id = str(uuid4())
    await invoke(
        app,
        "/api/v1/run/benchmark-boundary",
        method="POST",
        headers={"X-Langflow-Benchmark-Request-ID": request_id},
        send_hook=final_transport_send,
    )
    record = observer.consume(request_id)
    assert record["complete"] is True
    assert record["status_code"] == 200
    assert record["server_total_ms"] == 21


async def test_exception_is_preserved_and_record_marked_incomplete(enabled):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    observer = WorkerObserver()
    error = RuntimeError("sensitive path must not appear in measurement")

    async def downstream(scope, receive, send):
        raise error

    request_id = str(uuid4())
    with pytest.raises(RuntimeError) as caught:
        await invoke(
            BenchmarkWorkerIdentityMiddleware(downstream, observer=observer),
            "/api/v1/run/flow",
            headers={"X-Langflow-Benchmark-Request-ID": request_id},
        )
    assert caught.value is error
    record = observer.consume(request_id)
    assert record["complete"] is False
    assert "sensitive" not in json.dumps(record)


@pytest.mark.parametrize(
    ("change", "expected"), [("disabled", 404), ("remote", 403), ("token", 403), ("workers", 409), ("method", 405)]
)
async def test_control_endpoint_rejects_unsafe_access(enabled, monkeypatch, change, expected):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    async def downstream(scope, receive, send):
        pytest.fail("control request reached production application")

    headers = {"Authorization": "Bearer test-token"}
    peer, method = "127.0.0.1", "GET"
    if change == "disabled":
        monkeypatch.setenv("LANGFLOW_BENCHMARK_CONTROL_ENABLED", "false")
    elif change == "remote":
        peer = "203.0.113.9"
    elif change == "token":
        headers["Authorization"] = "Bearer wrong-token"
    elif change == "workers":
        monkeypatch.setenv("LANGFLOW_BENCHMARK_WORKER_COUNT", "2")
    elif change == "method":
        method = "POST"
    messages = await invoke(
        BenchmarkWorkerIdentityMiddleware(downstream, observer=WorkerObserver()),
        "/_benchmark/snapshot",
        headers=headers,
        peer=peer,
        method=method,
    )
    assert messages[0]["status"] == expected


async def test_measurement_endpoint_consumes_once(enabled):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    observer = WorkerObserver()
    request_id = str(uuid4())
    observer.finish(observer.begin(request_id), status_code=200, complete=True)

    async def downstream(scope, receive, send):
        pytest.fail("control request reached production application")

    app = BenchmarkWorkerIdentityMiddleware(downstream, observer=observer)
    path = f"/_benchmark/measurement/{request_id}"
    headers = {"Authorization": "Bearer test-token"}
    messages = await invoke(app, path, headers=headers)
    assert messages[0]["status"] == 200
    assert json.loads(messages[-1]["body"])["request_id"] == request_id
    assert (await invoke(app, path, headers=headers))[0]["status"] == 404


async def test_disabled_run_is_transparent(enabled, monkeypatch):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    monkeypatch.setenv("LANGFLOW_BENCHMARK_WORKER_IDENTITY_ENABLED", "false")
    observer = WorkerObserver()

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"unchanged"})

    request_id = str(uuid4())
    messages = await invoke(
        BenchmarkWorkerIdentityMiddleware(downstream, observer=observer),
        "/api/v1/run/flow",
        headers={"X-Langflow-Benchmark-Request-ID": request_id},
    )
    assert messages[0]["headers"] == []
    assert messages[-1]["body"] == b"unchanged"
    assert observer.consume(request_id) is None


async def test_component_body_interval_excludes_output_options_and_cache_hits():
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.custom.custom_component.component import Component
    from lfx.schema import Data
    from lfx.template.field.base import Output

    ticks = [0]
    observer = WorkerObserver(clock=lambda: ticks[0])

    class Example(Component):
        outputs = [Output(name="result", method="produce", display_name="Result")]

        def produce(self) -> Data:
            ticks[0] += 7_000_000
            return Data(data={"value": 123})

    component = Example(_id="CustomComponent-example")
    output = component._outputs_map["result"]
    original_options = output.apply_options

    def options(result):
        ticks[0] += 2_000_000
        return original_options(result)

    object.__setattr__(output, "apply_options", options)
    record = observer.begin("body")
    original_dispatch = Component._get_output_result
    with observer.instrumentation(), observer.bind(record):
        result = await component._get_output_result(output)
        assert (await component._get_output_result(output)) is result
    observer.finish(record, status_code=200, complete=True)
    measured = observer.consume("body")
    assert result.data == {"value": 123}
    assert measured["component_intervals_ms"] == [
        {"node_id": "CustomComponent-example", "start_ms": 0.0, "end_ms": 7.0},
    ]
    assert measured["server_total_ms"] == 9
    assert "produce" not in component.__dict__
    assert Component._get_output_result is original_dispatch


async def test_async_component_exception_retains_original_error_and_restores_method():
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.custom.custom_component.component import Component
    from lfx.schema import Data
    from lfx.template.field.base import Output

    observer = WorkerObserver()
    error = RuntimeError("failure")

    class Example(Component):
        outputs = [Output(name="result", method="produce", display_name="Result")]

        async def produce(self) -> Data:
            await asyncio.sleep(0)
            raise error

    component = Example(_id="CustomComponent-example")
    record = observer.begin("error")
    with observer.instrumentation(), observer.bind(record), pytest.raises(RuntimeError) as caught:
        await component._get_output_result(component._outputs_map["result"])
    observer.finish(record, status_code=500, complete=False)
    assert caught.value is error
    assert len(observer.consume("error")["component_intervals_ms"]) == 1
    assert "produce" not in component.__dict__


async def test_warm_counters_describe_actual_resolver_returns_and_errors(monkeypatch):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.api.v1 import endpoints

    results = iter([object(), None, RuntimeError("no")])

    async def resolver(*args, **kwargs):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(endpoints, "try_warm_run_graph", resolver)
    observer = WorkerObserver()
    with observer.instrumentation():
        for request_id, expected in [("one", "warm"), ("two", "cold"), ("three", "error")]:
            record = observer.begin(request_id)
            with observer.bind(record), contextlib.suppress(RuntimeError):
                await endpoints.try_warm_run_graph(None, None, user_id="u", context=None)
            observer.finish(record, status_code=200, complete=True)
            assert observer.consume(request_id)["warm_path"] == expected
        assert observer.warm_counters == {"attempts": 3, "hits": 1, "cold": 1, "errors": 1}
    assert endpoints.try_warm_run_graph is resolver


def test_snapshot_reports_only_approved_settings_and_process_counters(monkeypatch):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.services.warm_registry import service
    from lfx.custom import component_compilation_cache
    from lfx.services import deps

    settings = SimpleNamespace(
        component_compilation_cache_enabled=True,
        warm_registry_enabled=False,
        http_connection_reuse_enabled=False,
        secret_key="must-not-leak",
        database_url="sqlite:////private/path",
    )
    monkeypatch.setattr(deps, "get_settings_service", lambda: SimpleNamespace(settings=settings))
    monkeypatch.setattr(service, "get_warm_registry", lambda: [1, 2])
    snapshot = WorkerObserver().snapshot()
    assert snapshot["pid"] == os.getpid()
    assert snapshot["settings"] == {
        "component_compilation_cache_enabled": True,
        "warm_registry_enabled": False,
        "http_connection_reuse_enabled": False,
    }
    assert snapshot["compilation"] == component_compilation_cache.component_compilation_cache_stats()
    assert snapshot["registry_entries"] == 2
    assert "must-not-leak" not in json.dumps(snapshot)
    assert "/private/path" not in json.dumps(snapshot)


async def test_lifespan_restores_hooks_even_after_shutdown_failure(enabled):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.api.v1 import endpoints
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware
    from lfx.custom.custom_component.component import Component

    dispatch = Component._get_output_result
    warm = endpoints.try_warm_run_graph

    async def downstream(scope, receive, send):
        assert Component._get_output_result is not dispatch
        message = "shutdown failed"
        raise RuntimeError(message)

    app = BenchmarkWorkerIdentityMiddleware(downstream, observer=WorkerObserver())
    with pytest.raises(RuntimeError, match="shutdown failed"):
        await app({"type": "lifespan"}, None, None)
    assert Component._get_output_result is dispatch
    assert endpoints.try_warm_run_graph is warm


async def test_concurrent_benchmark_requests_are_rejected(enabled):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    entered = asyncio.Event()
    release = asyncio.Event()

    async def downstream(scope, receive, send):
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BenchmarkWorkerIdentityMiddleware(downstream, observer=WorkerObserver())
    first = asyncio.create_task(
        invoke(
            app,
            "/api/v1/run/flow",
            headers={
                "X-Langflow-Benchmark-Request-ID": str(uuid4()),
            },
        )
    )
    try:
        await entered.wait()
        second = await invoke(
            app,
            "/api/v1/run/flow",
            headers={
                "X-Langflow-Benchmark-Request-ID": str(uuid4()),
            },
        )
        assert second[0]["status"] == 409
    finally:
        release.set()
        await first


async def test_interval_overflow_marks_measurement_invalid_instead_of_growing_forever():
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.custom.custom_component.component import Component
    from lfx.schema import Data
    from lfx.template.field.base import Output

    class Example(Component):
        outputs = [Output(name="result", method="produce", display_name="Result", cache=False)]

        def produce(self) -> Data:
            return Data(data={"value": 1})

    observer = WorkerObserver(max_intervals=1)
    component = Example(_id="node")
    record = observer.begin("overflow")
    with observer.instrumentation(), observer.bind(record):
        await component._get_output_result(component._outputs_map["result"])
        await component._get_output_result(component._outputs_map["result"])
    observer.finish(record, status_code=200, complete=True)
    measured = observer.consume("overflow")
    assert len(measured["component_intervals_ms"]) == 1
    assert measured["intervals_truncated"] is True


async def test_no_request_context_leaves_components_and_counters_untouched(monkeypatch):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.api.v1 import endpoints
    from lfx.custom.custom_component.component import Component
    from lfx.schema import Data
    from lfx.template.field.base import Output

    class Example(Component):
        outputs = [Output(name="result", method="produce", display_name="Result")]

        def produce(self) -> Data:
            assert "produce" not in self.__dict__
            return Data(data={"untouched": True})

    sentinel = object()

    async def warm(*args, **kwargs):
        return sentinel

    monkeypatch.setattr(endpoints, "try_warm_run_graph", warm)
    observer = WorkerObserver()
    component = Example(_id="node")
    with observer.instrumentation():
        result = await component._get_output_result(component._outputs_map["result"])
        assert await endpoints.try_warm_run_graph(None) is sentinel
    assert result.data == {"untouched": True}
    assert observer.warm_counters == {"attempts": 0, "hits": 0, "cold": 0, "errors": 0}


async def test_non_ascii_control_header_fails_closed(enabled):
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    async def downstream(scope, receive, send):
        pytest.fail("unauthenticated request reached production application")

    messages = await invoke(
        BenchmarkWorkerIdentityMiddleware(downstream),
        "/_benchmark/snapshot",
        headers={
            "Authorization": "Bearer invalid-\u00e9",
        },
    )
    assert messages[0]["status"] == 403
