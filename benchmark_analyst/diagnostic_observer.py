"""Opt-in bounded worker intervals, separate from the primary ASGI clock.

Hooks retain names, identities and timestamps only. They perform no file, network,
SQL, process-memory or logging I/O. Payload byte accounting is not heap accounting.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import inspect
import os
import time
from collections import deque
from contextvars import ContextVar
from functools import wraps
from itertools import count
from threading import RLock, get_ident


def process_create_time() -> float | None:
    """Use psutil when installed; the runner owns the mandatory identity probe."""
    try:
        import psutil

        return psutil.Process(os.getpid()).create_time()
    except (ImportError, OSError):
        return None
    except Exception:  # psutil's platform-specific access exceptions are optional.
        return None


class DiagnosticObserver:
    """One bounded stream per worker; callers instantiate it only when enabled."""

    def __init__(
        self,
        *,
        detail="coarse",
        max_events=8192,
        max_open_events=1024,
        lag_interval_seconds=0.02,
        clock=time.perf_counter_ns,
    ):
        if detail not in {"coarse", "setup"}:
            raise ValueError("diagnostic detail must be coarse or setup")
        if max_events < 1 or max_open_events < 1 or lag_interval_seconds <= 0:
            raise ValueError("diagnostic bounds and interval must be positive")
        self.detail = detail
        self.clock = clock
        self.max_events = max_events
        self.max_open_events = max_open_events
        self.lag_interval_seconds = lag_interval_seconds
        self.pid = os.getpid()
        self.process_create_time = process_create_time()
        self._events = deque()
        self._open = {}
        self._lock = RLock()
        self._event_ids = count(1)
        self._dropped = 0
        self._truncated = 0
        self._drained = 0
        self._request = ContextVar("benchmark_diagnostic_request", default=None)
        self._request_state = ContextVar("benchmark_diagnostic_request_state", default=None)
        self._parent = ContextVar("benchmark_diagnostic_parent", default=None)
        self._lag_task = None
        self._gc_starts = {}
        self._lifespan_users = 0
        self.capabilities = {
            "setup": False,
            "components": True,
            "request_boundary": True,
            "pre_component": True,
            "component_body": True,
            "dispatch_wait": True,
            "gc": False,
            "event_loop_lag": False,
            "db_query": False,
            "db_checkout": False,
            "telemetry": False,
            "native_trace_finalization": False,
            "warm_reconciliation": False,
        }

    @contextlib.contextmanager
    def request_context(self, request_id, *, parent_id=None, request_state=None):
        request_token = self._request.set(request_id)
        state_token = self._request_state.set(request_state)
        parent_token = self._parent.set(parent_id)
        try:
            yield
        finally:
            self._parent.reset(parent_token)
            self._request_state.reset(state_token)
            self._request.reset(request_token)

    def _request_finished(self):
        state = self._request_state.get()
        return state is not None and state.finished

    def start_event(self, category, name, *, start_ns=None, request_id=None, parent_id=None):
        with self._lock:
            if len(self._open) >= self.max_open_events:
                self._dropped += 1
                self._truncated += 1
                return None
            # GC callbacks can reenter this RLock while an event is allocated.
            # Reserve a number once; formatting must not reread shared state.
            event_number = next(self._event_ids)
            event_id = f"{self.pid}:{event_number}"
            inherited_request = None if self._request_finished() else self._request.get()
            inherited_parent = None if self._request_finished() else self._parent.get()
            self._open[event_id] = {
                "schema_version": 1,
                "pid": self.pid,
                "process_create_time": self.process_create_time,
                "event_id": event_id,
                "request_id": inherited_request if request_id is None else request_id,
                "parent_id": inherited_parent if parent_id is None else parent_id,
                "category": category,
                "name": name,
                "start_ns": self.clock() if start_ns is None else start_ns,
                "thread_id": get_ident(),
            }
            return event_id

    def end_event(self, event_id, *, outcome="ok", end_ns=None, fields=None):
        if event_id is None:
            return
        with self._lock:
            event = self._open.pop(event_id, None)
            if event is None:
                return
            event["end_ns"] = self.clock() if end_ns is None else end_ns
            event["outcome"] = outcome
            # Only owned nuisance metrics are accepted, never function arguments.
            if fields:
                event.update(
                    {
                        key: value
                        for key, value in fields.items()
                        if key
                        in {
                            "generation",
                            "collected",
                            "uncollectable",
                            "lag_ns",
                            "node_id",
                        }
                    }
                )
            if len(self._events) >= self.max_events:
                self._dropped += 1
                self._truncated += 1
            else:
                self._events.append(event)

    @contextlib.contextmanager
    def span(self, category, name, *, start_ns=None, fields=None):
        event_id = self.start_event(category, name, start_ns=start_ns)
        token = self._parent.set(event_id if event_id is not None else self._parent.get())
        outcome = "ok"
        try:
            yield event_id
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "error"
            raise
        finally:
            self._parent.reset(token)
            self.end_event(event_id, outcome=outcome, fields=fields)

    def drain(self):
        with self._lock:
            # Reentrant GC appends to the active deque. Detach before copying
            # so an event emitted during this drain survives the next drain.
            completed = self._events
            self._events = deque()
            events = list(completed)
            self._drained += len(events)
            return {
                "events": events,
                "metadata": {
                    "schema_version": 1,
                    "enabled": True,
                    "detail": self.detail,
                    "clock": "worker_monotonic",
                    "unit": "ns",
                    "pid": self.pid,
                    "process_create_time": self.process_create_time,
                    "capabilities": dict(self.capabilities),
                    "capability_reasons": {
                        name: "not_instrumented" for name, enabled in self.capabilities.items() if not enabled
                    },
                    "dropped": self._dropped,
                    "truncated": self._truncated,
                    "unfinished": len(self._open),
                    "drained": len(events),
                    "total_drained": self._drained,
                    "max_events": self.max_events,
                    "max_open_events": self.max_open_events,
                    "lag_interval_ms": self.lag_interval_seconds * 1000,
                },
            }

    def _gc_callback(self, phase, info):
        generation = info.get("generation", -1)
        key = (get_ident(), generation)
        if phase == "start":
            self._gc_starts[key] = self.start_event("gc", "gc_collection")
        elif phase == "stop":
            self.end_event(
                self._gc_starts.pop(key, None),
                fields={
                    "generation": generation,
                    "collected": info.get("collected", 0),
                    "uncollectable": info.get("uncollectable", 0),
                },
            )

    async def _sample_lag(self):
        interval_ns = int(self.lag_interval_seconds * 1_000_000_000)
        while True:
            due = self.clock() + interval_ns
            await asyncio.sleep(self.lag_interval_seconds)
            now = self.clock()
            event_id = self.start_event("event_loop", "event_loop_lag", start_ns=due)
            self.end_event(event_id, end_ns=max(due, now), fields={"lag_ns": max(0, now - due)})

    @contextlib.asynccontextmanager
    async def lifespan(self):
        self._lifespan_users += 1
        if self._lifespan_users == 1:
            gc.callbacks.append(self._gc_callback)
            # The timer starts outside request context and inherits no request ID.
            self._lag_task = asyncio.create_task(self._sample_lag())
            self.capabilities["gc"] = True
            self.capabilities["event_loop_lag"] = True
        try:
            yield
        finally:
            self._lifespan_users -= 1
            if self._lifespan_users == 0:
                with contextlib.suppress(ValueError):
                    gc.callbacks.remove(self._gc_callback)
                task, self._lag_task = self._lag_task, None
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                for event_id in list(self._gc_starts.values()):
                    self.end_event(event_id, outcome="incomplete")
                self._gc_starts.clear()

    def _wrapper(self, function, category, name):
        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def wrapped(*args, **kwargs):
                if self._request.get() is None or self._request_finished():
                    return await function(*args, **kwargs)
                with self.span(category, name):
                    return await function(*args, **kwargs)
        else:

            @wraps(function)
            def wrapped(*args, **kwargs):
                if self._request.get() is None or self._request_finished():
                    return function(*args, **kwargs)
                with self.span(category, name):
                    return function(*args, **kwargs)

        return wrapped

    @contextlib.contextmanager
    def instrumentation(self):
        """Patch aliases called by v1; restore exact descriptors on every exit."""
        from langflow.api import warm_graph
        from langflow.api.v1 import endpoints, global_variable_defaults
        from lfx.custom import validate
        from lfx.extension import migration
        from lfx.extension.migration import events
        from lfx.graph.graph import base as graph_base

        hooks = [
            (endpoints, "get_flow_by_id_or_endpoint_name", "setup", "flow_fetch"),
            (endpoints, "prepare_flow_build_for_user", "setup", "caller_policy"),
            (endpoints, "apply_global_variable_defaults", "setup", "global_defaults"),
            (global_variable_defaults, "apply_global_variable_defaults", "setup", "global_defaults"),
            (endpoints, "ensure_flow_permission", "setup", "flow_permission"),
            (warm_graph, "warm_deepcopy", "setup", "warm_setup"),
            (endpoints, "run_graph_internal", "execution", "graph_execution"),
            (graph_base.Graph, "from_payload", "setup", "cold_setup"),
            (graph_base.Graph, "copy_for_run", "setup", "warm_graph_copy"),
            (migration, "migrate_flow_payload", "setup", "migration_validation"),
            (events, "report_migration", "setup", "migration_event_replay"),
            (validate, "create_class", "setup", "class_preparation"),
        ]
        if self.detail == "setup":
            hooks.extend(
                [
                    (graph_base, "process_flow", "setup", "process_flow"),
                    (graph_base.Graph, "add_nodes_and_edges", "setup", "vertices_edges_params"),
                    (graph_base.Graph, "_instantiate_components_in_vertices", "setup", "constructors"),
                    (validate, "_get_component_compilation_artifact", "setup", "artifact_lookup"),
                    (validate, "_load_component_module_template", "setup", "ast_restore"),
                    (validate, "prepare_global_scope", "setup", "module_preparation"),
                    (validate, "build_class_constructor", "setup", "class_exec"),
                    (validate, "register_compiled_class_method_returns", "setup", "annotation_registration"),
                ]
            )
        restored = []
        try:
            for owner, attribute, category, name in hooks:
                original = inspect.getattr_static(owner, attribute)
                if isinstance(original, classmethod):
                    wrapped = classmethod(self._wrapper(original.__func__, category, name))
                elif isinstance(original, staticmethod):
                    wrapped = staticmethod(self._wrapper(original.__func__, category, name))
                else:
                    wrapped = self._wrapper(original, category, name)
                setattr(owner, attribute, wrapped)
                restored.append((owner, attribute, original, wrapped))
                self.capabilities[name] = True
            self.capabilities["setup"] = True
            yield
        finally:
            for owner, attribute, original, wrapped in reversed(restored):
                if inspect.getattr_static(owner, attribute) is wrapped:
                    setattr(owner, attribute, original)
