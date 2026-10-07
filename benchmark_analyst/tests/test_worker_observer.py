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
    registry = service.WarmGraphRegistry()
    registry._flows.update({"one": object(), "two": object()})
    monkeypatch.setattr(service, "get_warm_registry", lambda: registry)
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
    assert snapshot["effective"]["database_path"] == "/private/path"
    assert "sqlite:" not in json.dumps(snapshot)


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


async def test_diagnostics_off_control_allocates_no_buffer_or_nuisance_hooks(enabled, monkeypatch):
    import gc

    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    monkeypatch.setenv("LANGFLOW_BENCHMARK_DIAGNOSTICS_ENABLED", "false")
    callbacks = list(gc.callbacks)
    tasks = asyncio.all_tasks()
    observer = WorkerObserver()
    assert observer.diagnostics is None

    async def downstream(scope, receive, send):
        assert gc.callbacks == callbacks
        assert asyncio.all_tasks() == tasks

    app = BenchmarkWorkerIdentityMiddleware(downstream, observer=observer)
    await app({"type": "lifespan"}, None, None)
    response = await invoke(app, "/_benchmark/diagnostics/drain", headers={"Authorization": "Bearer test-token"})
    result = json.loads(response[-1]["body"])
    assert result["events"] == []
    assert result["metadata"]["enabled"] is False
    assert result["metadata"]["dropped"] == 0
    assert result["metadata"]["unfinished"] == 0
    assert observer.diagnostics is None


async def test_diagnostics_body_and_dispatch_wait_use_existing_clock_and_do_not_capture_payload(monkeypatch):
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.custom.custom_component.component import Component
    from lfx.schema import Data
    from lfx.template.field.base import Output

    monkeypatch.setenv("LANGFLOW_BENCHMARK_DIAGNOSTICS_ENABLED", "true")
    ticks = [0]
    observer = WorkerObserver(clock=lambda: ticks[0])

    class Example(Component):
        outputs = [Output(name="result", method="produce", display_name="Result")]

        def produce(self) -> Data:
            ticks[0] += 7_000_000
            return Data(data={"private": "source-and-secret"})

    component = Example(_id="node")
    output = component._outputs_map["result"]
    original_options = output.apply_options

    def options(result):
        ticks[0] += 2_000_000
        return original_options(result)

    object.__setattr__(output, "apply_options", options)
    record = observer.begin("diagnostic-request")
    with observer.instrumentation(), observer.bind(record):
        result = await component._get_output_result(output)
    observer.finish(record, status_code=200, complete=True)
    assert result.data == {"private": "source-and-secret"}
    assert observer.consume("diagnostic-request")["component_intervals_ms"] == [
        {"node_id": "node", "start_ms": 0.0, "end_ms": 7.0},
    ]
    drained = observer.diagnostics_drain()
    events = {event["name"]: event for event in drained["events"]}
    assert events["component_body"]["end_ns"] - events["component_body"]["start_ns"] == 7_000_000
    assert events["dispatch_wait"]["end_ns"] == events["component_body"]["start_ns"]
    assert events["pre_component"]["end_ns"] == events["component_body"]["start_ns"]
    assert events["asgi_request"]["end_ns"] == 9_000_000
    assert drained["metadata"]["unfinished"] == 0
    assert "source-and-secret" not in json.dumps(drained)
    assert "produce" not in component.__dict__


async def test_effective_logging_override_is_applied_after_lifespan_startup(enabled, monkeypatch):
    import logging

    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    monkeypatch.setenv("LANGFLOW_BENCHMARK_ACCESS_LOG", "false")
    access = logging.getLogger("uvicorn.access")
    previous = access.level

    async def downstream(scope, receive, send):
        access.setLevel(logging.INFO)  # Production startup can reconfigure this logger.
        await send({"type": "lifespan.startup.complete"})

    async def send(message):
        assert message["type"] == "lifespan.startup.complete"
        assert not access.isEnabledFor(logging.INFO)

    try:
        await BenchmarkWorkerIdentityMiddleware(downstream, observer=WorkerObserver())(
            {"type": "lifespan"},
            None,
            send,
        )
    finally:
        access.setLevel(previous)


def test_snapshot_reports_accounting_and_allowlisted_effective_paths_without_credentials(monkeypatch, tmp_path):
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.services import deps

    settings = SimpleNamespace(
        component_compilation_cache_enabled=False,
        warm_registry_enabled=False,
        http_connection_reuse_enabled=False,
        config_dir=str(tmp_path),
        storage_type="local",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/fixture.db",
        do_not_track=True,
        deactivate_tracing=False,
    )
    monkeypatch.setattr(deps, "get_settings_service", lambda: SimpleNamespace(settings=settings))
    snapshot = WorkerObserver().snapshot()
    assert snapshot["effective"]["database_path"] == str(tmp_path / "fixture.db")
    assert snapshot["effective"]["config_dir"] == str(tmp_path)
    assert snapshot["effective"]["product_telemetry_enabled"] is False
    assert snapshot["effective"]["native_tracing"] is True
    assert snapshot["cache_accounting"]["compilation"]["heap_bytes"] is None
    assert snapshot["cache_accounting"]["warm"]["heap_bytes"] is None
    assert "versions" in snapshot["effective"]
    assert snapshot["process_create_time"] is None or snapshot["process_create_time"] > 0
    settings.database_url = "postgresql://private-user:secret-password@private-host/private-db"
    serialized = json.dumps(WorkerObserver().snapshot())
    assert "private-user" not in serialized and "secret-password" not in serialized
    assert "private-host" not in serialized and "private-db" not in serialized


@pytest.fixture
def snapshot_settings(monkeypatch):
    from langflow.services.warm_registry import service
    from lfx.services import deps

    settings = SimpleNamespace(
        database_url="sqlite:////misleading/settings/path.db",
        warm_reconcile_interval=17.5,
        warm_registry_max_entries=128,
        warm_registry_max_flow_bytes=2000000,
        warm_registry_max_total_bytes=32000000,
        warm_registry_preload_limit=0,
        telemetry_writer_batch_size=73,
        telemetry_writer_flush_interval_s=0.25,
        telemetry_writer_batch_size_bytes=131072,
        transactions_storage_enabled=True,
        vertex_builds_storage_enabled=False,
        sync_result_storage_enabled=False,
        use_noop_database=False,
        secret_key="private-secret",
    )
    monkeypatch.setattr(deps, "get_settings_service", lambda: SimpleNamespace(settings=settings))
    registry = service.WarmGraphRegistry()
    monkeypatch.setattr(service, "get_warm_registry", lambda: registry)
    return settings


async def test_async_snapshot_probes_actual_serving_engine_read_only(snapshot_settings, monkeypatch, tmp_path):
    from benchmark_analyst.fixtures import verify_fixture_worker
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.services import deps
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    database = tmp_path / "serving.db"
    snapshot_settings.config_dir = str(tmp_path)
    snapshot_settings.storage_type = "local"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}", poolclass=StaticPool)
    statements = []
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            await connection.exec_driver_sql("PRAGMA busy_timeout=731")
            await connection.exec_driver_sql("CREATE TABLE fixture (value INTEGER)")
        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement),
        )
        monkeypatch.setattr(deps, "get_db_service", lambda: SimpleNamespace(engine=engine))
        effective = (await WorkerObserver().snapshot_async())["effective"]
        assert effective["database_engine"]["status"] == "measured"
        assert effective["database_engine"]["dialect"] == "sqlite"
        assert effective["database_engine"]["driver"] == "aiosqlite"
        assert effective["database_engine"]["pool"]["class"] == "StaticPool"
        assert effective["sqlite_pragmas"]["status"] == "measured"
        assert effective["sqlite_pragmas"]["values"]["busy_timeout"] == 731
        assert effective["sqlite_pragmas"]["values"]["journal_mode"] == "wal"
        assert set(statements) == {
            f"PRAGMA {key}"
            for key in ("journal_mode", "synchronous", "wal_autocheckpoint", "page_size", "busy_timeout")
        }
        assert len(statements) == 5
        assert effective["database_path"] == str(database)
        assert effective["database_sizes"]["database"]["bytes"] == database.stat().st_size
        assert effective["database_sizes"]["wal"]["bytes"] >= 0
        assert effective["database_probe_duration_ms"] >= 0
        assert "private-secret" not in json.dumps(effective)
        verify_fixture_worker({"effective": effective}, {"database_path": str(database), "config_dir": str(tmp_path)})
    finally:
        await engine.dispose()


def test_sync_snapshot_retains_maintenance_allowlist_and_marks_component_message_flags_unavailable(snapshot_settings):
    from benchmark_analyst.worker_observer import WorkerObserver

    effective = WorkerObserver().snapshot()["effective"]
    maintenance = effective["maintenance_settings"]
    assert maintenance["warm_reconcile_interval"] == 17.5
    assert maintenance["telemetry_writer_batch_size"] == 73
    assert maintenance["telemetry_writer_flush_interval_s"] == 0.25
    assert maintenance["transactions_storage_enabled"] is True
    assert maintenance["vertex_builds_storage_enabled"] is False
    assert maintenance["message_storage"]["status"] == "unavailable"
    assert maintenance["message_storage"]["reason"] == "component_and_request_scoped"
    assert "secret_key" not in maintenance


async def test_async_snapshot_engine_errors_are_explicit_without_leaking_exception_text(snapshot_settings, monkeypatch):
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.services import deps

    def unavailable():
        raise RuntimeError("postgresql://private-user:password@private-host/database")

    monkeypatch.setattr(deps, "get_db_service", unavailable)
    effective = (await WorkerObserver().snapshot_async())["effective"]
    assert effective["database_engine"]["status"] == "unavailable"
    assert effective["database_engine"]["reason"] == "RuntimeError"
    assert effective["sqlite_pragmas"]["status"] == "unavailable"
    assert "password" not in json.dumps(effective)


@pytest.mark.parametrize("engine_state", ["missing", "sync", "lookup_error"])
async def test_async_snapshot_cannot_prove_fixture_path_from_settings_only(
    snapshot_settings, monkeypatch, tmp_path, engine_state
):
    from benchmark_analyst.fixtures import verify_fixture_worker
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.services import deps
    from sqlalchemy import create_engine

    database = tmp_path / "configured-clone.db"
    database.touch()
    snapshot_settings.database_url = f"sqlite:///{database}"
    snapshot_settings.config_dir = str(tmp_path)
    snapshot_settings.storage_type = "local"
    engine = create_engine("sqlite:///:memory:") if engine_state == "sync" else None

    def database_service():
        if engine_state == "lookup_error":
            raise RuntimeError("private database lookup failed")
        return SimpleNamespace(engine=engine)

    monkeypatch.setattr(deps, "get_db_service", database_service)
    try:
        snapshot = await WorkerObserver().snapshot_async()
        effective = snapshot["effective"]
        assert effective["database_path"] is None
        assert effective["database_backend"] is None
        assert effective["database_engine"]["status"] == "unavailable"
        with pytest.raises(ValueError, match="SQLite|engine|fixture"):
            verify_fixture_worker(snapshot, {"database_path": str(database), "config_dir": str(tmp_path)})
    finally:
        if engine is not None:
            engine.dispose()


async def test_async_snapshot_unsupported_engine_cannot_retain_configured_fixture_path(
    snapshot_settings, monkeypatch, tmp_path
):
    from benchmark_analyst.fixtures import verify_fixture_worker
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.services import deps
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
    from sqlalchemy.engine import Engine, make_url
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.pool import NullPool

    database = tmp_path / "configured-clone.db"
    database.touch()
    snapshot_settings.database_url = f"sqlite:///{database}"
    snapshot_settings.config_dir = str(tmp_path)
    snapshot_settings.storage_type = "local"
    # A real unsupported AsyncEngine needs no installed driver or network probe.
    engine = AsyncEngine(
        Engine(
            NullPool(lambda: pytest.fail("unsupported engine must not open a connection")),
            PGDialect_asyncpg(),
            make_url("postgresql+asyncpg://private-user:private-password@localhost/source"),
        )
    )
    monkeypatch.setattr(deps, "get_db_service", lambda: SimpleNamespace(engine=engine))
    try:
        snapshot = await WorkerObserver().snapshot_async()
        effective = snapshot["effective"]
        assert effective["database_path"] is None
        assert effective["database_backend"] == "postgresql"
        assert effective["database_engine"]["status"] == "measured"
        assert effective["sqlite_pragmas"]["reason"] == "unsupported_dialect"
        assert "private-password" not in json.dumps(effective)
        with pytest.raises(ValueError, match="SQLite|engine|fixture"):
            verify_fixture_worker(snapshot, {"database_path": str(database), "config_dir": str(tmp_path)})
    finally:
        await engine.dispose()


async def test_async_snapshot_in_memory_sqlite_cannot_prove_fixture_path(snapshot_settings, monkeypatch, tmp_path):
    from benchmark_analyst.fixtures import verify_fixture_worker
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.services import deps
    from sqlalchemy.ext.asyncio import create_async_engine

    snapshot_settings.config_dir = str(tmp_path)
    snapshot_settings.storage_type = "local"
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(deps, "get_db_service", lambda: SimpleNamespace(engine=engine))
    try:
        snapshot = await WorkerObserver().snapshot_async()
        assert snapshot["effective"]["database_engine"]["status"] == "measured"
        assert snapshot["effective"]["database_path"] is None
        with pytest.raises(ValueError, match="fixture|path"):
            verify_fixture_worker(snapshot, {"database_path": str(tmp_path / "clone.db"), "config_dir": str(tmp_path)})
    finally:
        await engine.dispose()


async def test_async_snapshot_does_not_recreate_a_missing_database(snapshot_settings, monkeypatch, tmp_path):
    from benchmark_analyst.worker_observer import WorkerObserver
    from lfx.services import deps
    from sqlalchemy.ext.asyncio import create_async_engine

    database = tmp_path / "missing.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    monkeypatch.setattr(deps, "get_db_service", lambda: SimpleNamespace(engine=engine))
    try:
        effective = (await WorkerObserver().snapshot_async())["effective"]
        assert not database.exists()
        assert effective["sqlite_pragmas"]["status"] == "unavailable"
        assert effective["sqlite_pragmas"]["reason"] == "database_file_missing"
    finally:
        await engine.dispose()


async def test_authenticated_snapshot_enriches_database_only_after_authorization(enabled, monkeypatch):
    from benchmark_analyst.worker_observer import WorkerObserver
    from langflow.benchmark_worker_identity import BenchmarkWorkerIdentityMiddleware

    calls = []
    observer = WorkerObserver()

    async def snapshot_async():
        calls.append("database_probe")
        return {"pid": os.getpid(), "effective": {"sqlite_pragmas": {"status": "measured"}}}

    monkeypatch.setattr(observer, "snapshot_async", snapshot_async, raising=False)

    async def downstream(scope, receive, send):
        pytest.fail("control reached production app")

    app = BenchmarkWorkerIdentityMiddleware(downstream, observer=observer)
    messages = await invoke(app, "/_benchmark/snapshot", headers={"Authorization": "Bearer wrong-token"})
    assert messages[0]["status"] == 403
    assert calls == []
    messages = await invoke(app, "/_benchmark/snapshot", headers={"Authorization": "Bearer test-token"})
    assert messages[0]["status"] == 200
    assert calls == ["database_probe"]
    assert json.loads(messages[1]["body"])["effective"]["sqlite_pragmas"]["status"] == "measured"
