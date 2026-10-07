"""Bounded, process-local evidence for the explicitly enabled SCRFD benchmark.

The observer neither runs nor changes a graph. Its dispatch wrapper temporarily
wraps the chosen bound output method on a request-local component, retaining the
original dispatcher (cache, to_thread, exception translation and options). The
sync clock therefore runs inside the existing executor thread, not around queue
wait. Component methods and process hooks are restored even on cancellation.
"""

# This opt-in observer hooks the real component dispatcher without duplicating it.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import gc
import inspect
import logging
import os
import platform
import sys
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from importlib.metadata import PackageNotFoundError, version
from threading import RLock
from typing import Any

SQLITE_PROVENANCE_PRAGMAS = ("journal_mode", "synchronous", "wal_autocheckpoint", "page_size", "busy_timeout")
MAINTENANCE_SETTINGS = (
    "warm_reconcile_interval",
    "warm_registry_max_entries",
    "warm_registry_max_flow_bytes",
    "warm_registry_max_total_bytes",
    "warm_registry_preload_limit",
    "telemetry_writer_batch_size",
    "telemetry_writer_flush_interval_s",
    "telemetry_writer_batch_size_bytes",
    "telemetry_writer_cleanup_interval_s",
    "transactions_storage_enabled",
    "vertex_builds_storage_enabled",
    "sync_result_storage_enabled",
    "use_noop_database",
)


def _database_file_size(path: str | None, *, absent_is_zero: bool = False) -> dict[str, Any]:
    if path is None:
        return {"status": "unavailable", "bytes": None, "reason": "no_local_database_file"}
    try:
        return {"status": "measured", "bytes": os.stat(path).st_size, "reason": None}
    except FileNotFoundError:
        if absent_is_zero:
            return {"status": "measured", "bytes": 0, "reason": "file_absent"}
        return {"status": "unavailable", "bytes": None, "reason": "FileNotFoundError"}
    except OSError as exc:
        return {"status": "unavailable", "bytes": None, "reason": type(exc).__name__}


def _pool_configuration(pool) -> dict[str, Any]:
    result = {"class": type(pool).__name__}
    for name, getter in (("size", "size"), ("timeout_seconds", "timeout")):
        try:
            method = getattr(pool, getter, None)
            result[name] = method() if callable(method) else None
            result[f"{name}_reason"] = None if callable(method) else "not_supported"
        except Exception as exc:
            result[name] = None
            result[f"{name}_reason"] = type(exc).__name__
    return result


async def _serving_database_provenance() -> dict[str, Any]:
    """Read the registered serving engine; never construct an alternate connection."""
    result = {
        "database_engine": {"status": "unavailable", "reason": "engine_unavailable"},
        # These describe the serving engine, never a settings-derived fallback.
        "database_backend": None,
        "database_path": None,
        "sqlite_pragmas": {"status": "unavailable", "reason": "engine_unavailable", "values": {}},
        "database_sizes": {
            "database": _database_file_size(None),
            "wal": _database_file_size(None),
        },
    }
    started = time.perf_counter()
    try:
        from lfx.services.deps import get_db_service
        from sqlalchemy.ext.asyncio import AsyncEngine

        engine = getattr(get_db_service(), "engine", None)
        if not isinstance(engine, AsyncEngine):
            return result
        dialect = engine.dialect.name
        result["database_engine"] = {
            "status": "measured",
            "reason": None,
            "dialect": dialect,
            "driver": engine.dialect.driver,
            "pool": _pool_configuration(engine.pool),
        }
        result["database_backend"] = dialect
        if dialect != "sqlite":
            result["sqlite_pragmas"]["reason"] = "unsupported_dialect"
            return result
        database = engine.url.database
        local_path = (
            os.path.realpath(database)
            if database and database != ":memory:" and not engine.url.query.get("uri")
            else None
        )
        result["database_path"] = local_path
        result["database_sizes"] = {
            "database": _database_file_size(local_path),
            "wal": _database_file_size(f"{local_path}-wal" if local_path else None, absent_is_zero=True),
        }
        if local_path and result["database_sizes"]["database"]["reason"] == "FileNotFoundError":
            # SQLite would create a new file when a pooled connection is opened.
            result["sqlite_pragmas"]["reason"] = "database_file_missing"
            return result
        values = {}
        async with engine.connect() as connection:
            for name in SQLITE_PROVENANCE_PRAGMAS:
                value = (await connection.exec_driver_sql(f"PRAGMA {name}")).scalar_one()
                values[name] = value
        result["sqlite_pragmas"] = {"status": "measured", "reason": None, "values": values}
    except Exception as exc:
        # Driver exception text can include URLs or credentials; keep its class only.
        result["sqlite_pragmas"] = {"status": "unavailable", "reason": type(exc).__name__, "values": {}}
        if result["database_engine"]["status"] != "measured":
            result["database_engine"]["reason"] = type(exc).__name__
    finally:
        result["database_probe_duration_ms"] = (time.perf_counter() - started) * 1000
    return result


def interval_union_ms(intervals: list[tuple[float, float]]) -> float:
    """Wall time covered by intervals; concurrent/nested work counts once."""
    total = 0.0
    right = float("-inf")
    for start, end in sorted(intervals):
        if end < start:
            message = "interval ends before it starts"
            raise ValueError(message)
        total += max(0.0, end - max(start, right))
        right = max(right, end)
    return total


@dataclass
class _Measurement:
    request_id: str
    start_ns: int
    component_intervals_ms: list[dict[str, Any]] = field(default_factory=list)
    warm_path: str | None = None
    intervals_truncated: bool = False
    finished: bool = False


@dataclass
class _DiagnosticMeasurement(_Measurement):
    diagnostic_request_event: str | None = None
    diagnostic_pre_event: str | None = None


class WorkerObserver:
    """Collect measurements without retaining component inputs, outputs or source."""

    def __init__(self, *, max_records: int = 256, max_intervals: int = 1024, clock=time.perf_counter_ns):
        if max_records < 1 or max_intervals < 1:
            message = "observer bounds must be positive"
            raise ValueError(message)
        self.clock = clock
        self._max_records = max_records
        self._max_intervals = max_intervals
        self._records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._current: ContextVar[_Measurement | None] = ContextVar("benchmark_measurement", default=None)
        self._lock = RLock()
        self._instrumentation_users = 0
        self.warm_counters = {"attempts": 0, "hits": 0, "cold": 0, "errors": 0}
        self._created_monotonic = time.monotonic()
        # Off means no diagnostic instance, buffers, ContextVars or nuisance task.
        self.diagnostics = None
        if os.environ.get("LANGFLOW_BENCHMARK_DIAGNOSTICS_ENABLED", "").lower() == "true":
            from benchmark_analyst.diagnostic_observer import DiagnosticObserver

            self.diagnostics = DiagnosticObserver(
                detail=os.environ.get("LANGFLOW_BENCHMARK_DIAGNOSTIC_DETAIL", "coarse"),
                clock=clock,
            )

    def begin(self, request_id: str, *, start_ns: int | None = None) -> _Measurement:
        record_class = _Measurement if self.diagnostics is None else _DiagnosticMeasurement
        record = record_class(request_id=request_id, start_ns=self.clock() if start_ns is None else start_ns)
        if self.diagnostics is not None:
            record.diagnostic_request_event = self.diagnostics.start_event(
                "request",
                "asgi_request",
                request_id=request_id,
                start_ns=record.start_ns,
            )
            record.diagnostic_pre_event = self.diagnostics.start_event(
                "setup",
                "pre_component",
                request_id=request_id,
                parent_id=record.diagnostic_request_event,
                start_ns=record.start_ns,
            )
        return record

    @contextmanager
    def bind(self, record: _Measurement):
        token = self._current.set(record)
        try:
            if self.diagnostics is None:
                yield
            else:
                with self.diagnostics.request_context(
                    record.request_id,
                    parent_id=record.diagnostic_request_event,
                    request_state=record,
                ):
                    yield
        finally:
            self._current.reset(token)

    def finish(self, record: _Measurement, *, status_code: int | None, complete: bool) -> None:
        end_ns = self.clock()
        with self._lock:
            if record.finished:
                return
            record.finished = True
            if self.diagnostics is not None:
                outcome = "ok" if complete else "incomplete"
                self.diagnostics.end_event(record.diagnostic_pre_event, outcome=outcome, end_ns=end_ns)
                self.diagnostics.end_event(record.diagnostic_request_event, outcome=outcome, end_ns=end_ns)
            self._records[record.request_id] = {
                "request_id": record.request_id,
                "pid": os.getpid(),
                "server_total_ms": (end_ns - record.start_ns) / 1_000_000,
                "component_intervals_ms": list(record.component_intervals_ms),
                "warm_path": record.warm_path,
                "status_code": status_code,
                "complete": complete,
                "intervals_truncated": record.intervals_truncated,
            }
            self._records.move_to_end(record.request_id)
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)

    def consume(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._records.pop(request_id, None)

    def _append_interval(self, record: _Measurement, node_id: str, start_ns: int, end_ns: int) -> None:
        with self._lock:
            if record.finished:
                return
            if len(record.component_intervals_ms) >= self._max_intervals:
                record.intervals_truncated = True
                return
            record.component_intervals_ms.append(
                {
                    "node_id": node_id,
                    "start_ms": (start_ns - record.start_ns) / 1_000_000,
                    "end_ms": (end_ns - record.start_ns) / 1_000_000,
                }
            )

    def _diagnostic_method_start(self, record, node_id, dispatch_event, started):
        diagnostic = self.diagnostics
        if diagnostic is None:
            return None
        diagnostic.end_event(record.diagnostic_pre_event, end_ns=started)
        diagnostic.end_event(dispatch_event, end_ns=started, fields={"node_id": node_id})
        return diagnostic.start_event("component", "component_body", start_ns=started)

    def _timed_method(self, method, record: _Measurement, node_id: str, dispatch_event=None):
        if self.diagnostics is not None:
            return self._diagnostic_timed_method(method, record, node_id, dispatch_event)
        if inspect.iscoroutinefunction(method):

            @wraps(method)
            async def timed_async(*args, **kwargs):
                started = self.clock()
                try:
                    return await method(*args, **kwargs)
                finally:
                    ended = self.clock()
                    self._append_interval(record, node_id, started, ended)

            return timed_async

        @wraps(method)
        def timed_sync(*args, **kwargs):
            started = self.clock()
            try:
                return method(*args, **kwargs)
            finally:
                ended = self.clock()
                self._append_interval(record, node_id, started, ended)

        return timed_sync

    def _diagnostic_timed_method(self, method, record, node_id, dispatch_event):
        if inspect.iscoroutinefunction(method):

            @wraps(method)
            async def timed_async(*args, **kwargs):
                started = self.clock()
                diagnostic_event = self._diagnostic_method_start(record, node_id, dispatch_event, started)
                outcome = "ok"
                try:
                    return await method(*args, **kwargs)
                except BaseException as exc:
                    outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                    raise
                finally:
                    ended = self.clock()
                    self._append_interval(record, node_id, started, ended)
                    self.diagnostics.end_event(
                        diagnostic_event, outcome=outcome, end_ns=ended, fields={"node_id": node_id}
                    )

            return timed_async

        @wraps(method)
        def timed_sync(*args, **kwargs):
            started = self.clock()
            diagnostic_event = self._diagnostic_method_start(record, node_id, dispatch_event, started)
            outcome = "ok"
            try:
                return method(*args, **kwargs)
            except BaseException as exc:
                outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                raise
            finally:
                ended = self.clock()
                self._append_interval(record, node_id, started, ended)
                self.diagnostics.end_event(diagnostic_event, outcome=outcome, end_ns=ended, fields={"node_id": node_id})

        return timed_sync

    @contextmanager
    def instrumentation(self):
        """Install transparent hooks for a benchmark worker's ASGI lifespan."""
        if self._instrumentation_users:
            self._instrumentation_users += 1
            try:
                yield
            finally:
                self._instrumentation_users -= 1
            return

        from langflow.api.v1 import endpoints
        from lfx.custom.custom_component.component import Component
        from lfx.template.field.base import UNDEFINED

        original_dispatch = Component._get_output_result
        original_warm = endpoints.try_warm_run_graph

        @wraps(original_dispatch)
        async def dispatch(component, output):
            record = self._current.get()
            if (
                record is None
                or record.finished
                or (output.cache and output.value != UNDEFINED)
                or output.method is None
            ):
                return await original_dispatch(component, output)
            method_name = output.method
            method = getattr(component, method_name)
            # Components are request-local on both production cold/warm paths.
            # Preserve a pre-existing instance override and class descriptor lookup.
            had_override = method_name in component.__dict__
            original_override = component.__dict__.get(method_name)
            dispatch_event = (
                self.diagnostics.start_event("dispatch", "dispatch_wait") if self.diagnostics is not None else None
            )
            wrapped = self._timed_method(method, record, str(component._id), dispatch_event)
            object.__setattr__(component, method_name, wrapped)
            try:
                return await original_dispatch(component, output)
            finally:
                if self.diagnostics is not None:
                    self.diagnostics.end_event(
                        dispatch_event, outcome="incomplete", fields={"node_id": str(component._id)}
                    )
                if component.__dict__.get(method_name) is wrapped:
                    if had_override:
                        object.__setattr__(component, method_name, original_override)
                    else:
                        object.__delattr__(component, method_name)

        @wraps(original_warm)
        async def warm(*args, **kwargs):
            record = self._current.get()
            if record is None:
                return await original_warm(*args, **kwargs)
            self.warm_counters["attempts"] += 1
            try:
                if self.diagnostics is None:
                    result = await original_warm(*args, **kwargs)
                else:
                    with self.diagnostics.span("setup", "warm_compatibility"):
                        result = await original_warm(*args, **kwargs)
            except BaseException:
                self.warm_counters["errors"] += 1
                record.warm_path = "error"
                raise
            if result is None:
                self.warm_counters["cold"] += 1
                record.warm_path = "cold"
            else:
                self.warm_counters["hits"] += 1
                record.warm_path = "warm"
            return result

        Component._get_output_result = dispatch
        endpoints.try_warm_run_graph = warm
        self._instrumentation_users = 1
        try:
            if self.diagnostics is None:
                yield
            else:
                with self.diagnostics.instrumentation():
                    yield
        finally:
            self._instrumentation_users -= 1
            if Component._get_output_result is dispatch:
                Component._get_output_result = original_dispatch
            if endpoints.try_warm_run_graph is warm:
                endpoints.try_warm_run_graph = original_warm

    def diagnostics_drain(self) -> dict[str, Any]:
        """The authenticated control has identical shape even in observer-off runs."""
        if self.diagnostics is not None:
            return self.diagnostics.drain()
        return {
            "events": [],
            "metadata": {
                "schema_version": 1,
                "enabled": False,
                "detail": os.environ.get("LANGFLOW_BENCHMARK_DIAGNOSTIC_DETAIL", "coarse"),
                "clock": "worker_monotonic",
                "unit": "ns",
                "pid": os.getpid(),
                "capabilities": {},
                "dropped": 0,
                "truncated": 0,
                "unfinished": 0,
                "drained": 0,
                "total_drained": 0,
            },
        }

    @staticmethod
    def apply_access_logging_override() -> None:
        """Reapply the benchmark's logger policy after production startup."""
        if os.environ.get("LANGFLOW_BENCHMARK_ACCESS_LOG", "").lower() == "false":
            logging.getLogger("uvicorn.access").setLevel(logging.ERROR)

    def snapshot(self) -> dict[str, Any]:
        """Read effective singleton state in this serving process, with an allowlist."""
        from langflow.services.warm_registry.service import get_warm_registry
        from lfx.custom.component_compilation_cache import (
            component_compilation_cache_accounting,
            component_compilation_cache_stats,
        )
        from lfx.services.deps import get_settings_service

        settings = get_settings_service().settings
        from benchmark_analyst.diagnostic_observer import process_create_time

        registry = get_warm_registry()
        db_path = None
        database_url = getattr(settings, "database_url", None)
        if isinstance(database_url, str) and database_url.startswith("sqlite"):
            from sqlalchemy.engine import make_url

            database = make_url(database_url).database
            if database and database != ":memory:":
                db_path = os.path.realpath(database)
        versions = {"python": platform.python_version()}
        for package in ("langflow", "langflow-base", "lfx", "uvicorn", "psutil", "onnxruntime", "numpy"):
            try:
                versions[package] = version(package)
            except PackageNotFoundError:
                versions[package] = None
        access = logging.getLogger("uvicorn.access")
        config_dir = getattr(settings, "config_dir", None)
        onnxruntime = sys.modules.get("onnxruntime")
        providers = onnxruntime.get_available_providers() if onnxruntime is not None else None
        process_threads = None
        try:
            import psutil

            process_threads = psutil.Process(os.getpid()).num_threads()
        except (ImportError, OSError):
            pass
        except Exception:
            process_threads = None
        return {
            "pid": os.getpid(),
            "process_create_time": process_create_time(),
            "worker_count": int(os.environ.get("LANGFLOW_BENCHMARK_WORKER_COUNT", "0")),
            "settings": {
                name: bool(getattr(settings, name, False))
                for name in (
                    "component_compilation_cache_enabled",
                    "warm_registry_enabled",
                    "http_connection_reuse_enabled",
                )
            },
            "compilation": component_compilation_cache_stats(),
            "warm": dict(self.warm_counters),
            "registry_entries": len(registry),
            "cache_accounting": {
                "compilation": component_compilation_cache_accounting(),
                "warm": registry.accounting_snapshot(),
            },
            "effective": {
                "database_path": db_path,
                "database_backend": "sqlite"
                if isinstance(database_url, str) and database_url.startswith("sqlite")
                else "other",
                "config_dir": os.path.realpath(config_dir) if config_dir else None,
                "storage_type": getattr(settings, "storage_type", None),
                "product_telemetry_enabled": not bool(getattr(settings, "do_not_track", False)),
                "native_tracing": not bool(getattr(settings, "deactivate_tracing", False))
                and os.environ.get("LANGFLOW_NATIVE_TRACING", "true").lower() not in {"false", "0", "no"},
                "telemetry_writer_enabled": bool(getattr(settings, "telemetry_writer_enabled", False)),
                "maintenance_settings": {
                    **{name: getattr(settings, name, None) for name in MAINTENANCE_SETTINGS},
                    "message_storage": {"status": "unavailable", "reason": "component_and_request_scoped"},
                },
                "uvicorn_access_enabled": access.isEnabledFor(logging.INFO),
                "uvicorn_access_level": logging.getLevelName(access.getEffectiveLevel()),
                "uvicorn_access_handlers": [type(handler).__name__ for handler in access.handlers],
                "uvicorn_access_propagate": access.propagate,
                "gc_enabled": gc.isenabled(),
                "gc_thresholds": list(gc.get_threshold()),
                "gc_counts": list(gc.get_count()),
                "process_threads": process_threads,
                "worker_uptime_seconds": time.monotonic() - self._created_monotonic,
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
                "onnx_available_providers": providers,
                "onnx_session_options": {"status": "unavailable", "reason": "request_local_options_not_observed"},
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

    async def snapshot_async(self) -> dict[str, Any]:
        """Enrich authenticated control evidence outside the primary timing path."""
        snapshot = self.snapshot()
        snapshot["effective"].update(await _serving_database_provenance())
        return snapshot
