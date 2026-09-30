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

import inspect
import os
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from threading import RLock
from typing import Any


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

    def begin(self, request_id: str, *, start_ns: int | None = None) -> _Measurement:
        return _Measurement(request_id=request_id, start_ns=self.clock() if start_ns is None else start_ns)

    @contextmanager
    def bind(self, record: _Measurement):
        token = self._current.set(record)
        try:
            yield
        finally:
            self._current.reset(token)

    def finish(self, record: _Measurement, *, status_code: int | None, complete: bool) -> None:
        end_ns = self.clock()
        with self._lock:
            if record.finished:
                return
            record.finished = True
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

    def _timed_method(self, method, record: _Measurement, node_id: str):
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
            if record is None or (output.cache and output.value != UNDEFINED) or output.method is None:
                return await original_dispatch(component, output)
            method_name = output.method
            method = getattr(component, method_name)
            # Components are request-local on both production cold/warm paths.
            # Preserve a pre-existing instance override and class descriptor lookup.
            had_override = method_name in component.__dict__
            original_override = component.__dict__.get(method_name)
            wrapped = self._timed_method(method, record, str(component._id))
            object.__setattr__(component, method_name, wrapped)
            try:
                return await original_dispatch(component, output)
            finally:
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
            yield
        finally:
            self._instrumentation_users -= 1
            if Component._get_output_result is dispatch:
                Component._get_output_result = original_dispatch
            if endpoints.try_warm_run_graph is warm:
                endpoints.try_warm_run_graph = original_warm

    def snapshot(self) -> dict[str, Any]:
        """Read effective singleton state in this serving process, with an allowlist."""
        from langflow.services.warm_registry.service import get_warm_registry
        from lfx.custom.component_compilation_cache import component_compilation_cache_stats
        from lfx.services.deps import get_settings_service

        settings = get_settings_service().settings
        return {
            "pid": os.getpid(),
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
            "registry_entries": len(get_warm_registry()),
        }
