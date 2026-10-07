"""Bounded diagnostic evidence must preserve execution and context semantics."""

# ruff: noqa: ARG001, INP001, PLR2004, S101, SLF001

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import os
import sys
from types import SimpleNamespace

import pytest


def test_gc_reentry_during_id_formatting_keeps_both_events_unique():
    from benchmark_analyst.diagnostic_observer import DiagnosticObserver

    observer = DiagnosticObserver()
    source, first_line = inspect.getsourcelines(DiagnosticObserver.start_event)
    formatting_line = first_line + next(i for i, line in enumerate(source) if "event_id = f" in line)
    previous_trace = sys.gettrace()
    triggered = False

    def collect_between_reservation_and_formatting(frame, event, arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is DiagnosticObserver.start_event.__code__
            and frame.f_locals.get("name") == "global_defaults"
            and frame.f_lineno == formatting_line
        ):
            triggered = True
            gc.collect(0)
        return collect_between_reservation_and_formatting

    callback = observer._gc_callback
    gc.callbacks.append(callback)
    try:
        sys.settrace(collect_between_reservation_and_formatting)
        event_id = observer.start_event("setup", "global_defaults")
        observer.end_event(event_id)
    finally:
        sys.settrace(previous_trace)
        gc.callbacks.remove(callback)
    result = observer.drain()
    assert triggered
    assert any(event["category"] == "gc" for event in result["events"])
    assert any(event["name"] == "global_defaults" for event in result["events"])
    assert len({event["event_id"] for event in result["events"]}) == len(result["events"])


def test_gc_reentry_after_drain_copy_keeps_event_for_next_drain():
    from benchmark_analyst.diagnostic_observer import DiagnosticObserver

    observer = DiagnosticObserver()
    before = observer.start_event("setup", "before_drain")
    observer.end_event(before)
    source, first_line = inspect.getsourcelines(DiagnosticObserver.drain)
    after_copy_line = first_line + next(i for i, line in enumerate(source) if "events = list(" in line) + 1
    previous_trace = sys.gettrace()
    gc_was_enabled = gc.isenabled()
    triggered = False

    def collect_after_buffer_copy(frame, event, arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is DiagnosticObserver.drain.__code__
            and frame.f_lineno == after_copy_line
        ):
            triggered = True
            gc.collect(0)
        return collect_after_buffer_copy

    callback = observer._gc_callback
    gc.disable()
    gc.callbacks.append(callback)
    try:
        sys.settrace(collect_after_buffer_copy)
        first = observer.drain()
    finally:
        sys.settrace(previous_trace)
        gc.callbacks.remove(callback)
        if gc_was_enabled:
            gc.enable()
    second = observer.drain()
    assert triggered
    assert [event["name"] for event in first["events"]] == ["before_drain"]
    assert [event["name"] for event in second["events"]] == ["gc_collection"]
    assert second["metadata"]["total_drained"] == 2
    assert second["metadata"]["dropped"] == 0


def test_nested_spans_preserve_parentage_and_do_not_capture_values():
    from benchmark_analyst.diagnostic_observer import DiagnosticObserver

    ticks = [100]
    observer = DiagnosticObserver(clock=lambda: ticks[0])
    with observer.request_context("request-one", parent_id=None):
        with observer.span("setup", "cold_setup"):
            ticks[0] = 200
            with observer.span("setup", "class_preparation"):
                ticks[0] = 300
            ticks[0] = 400
    events = observer.drain()["events"]
    parent = next(event for event in events if event["name"] == "cold_setup")
    child = next(event for event in events if event["name"] == "class_preparation")
    assert child["parent_id"] == parent["event_id"]
    assert (parent["start_ns"], parent["end_ns"]) == (100, 400)
    assert (child["start_ns"], child["end_ns"]) == (200, 300)
    assert all(event["request_id"] == "request-one" and event["pid"] == os.getpid() for event in events)
    assert observer.drain()["events"] == []


async def test_cancellation_and_thread_context_preserve_outcomes_and_restore_bindings():
    from benchmark_analyst.diagnostic_observer import DiagnosticObserver

    observer = DiagnosticObserver()

    def threaded():
        with observer.span("component", "thread_body"):
            return 17

    with observer.request_context("request-one", parent_id=None):
        with observer.span("setup", "outer"):
            assert await asyncio.to_thread(threaded) == 17
        with pytest.raises(asyncio.CancelledError):
            with observer.span("setup", "cancelled_setup"):
                raise asyncio.CancelledError
    with observer.span("background", "after_request"):
        pass
    events = {event["name"]: event for event in observer.drain()["events"]}
    assert events["thread_body"]["parent_id"] == events["outer"]["event_id"]
    assert events["thread_body"]["request_id"] == "request-one"
    assert events["cancelled_setup"]["outcome"] == "cancelled"
    assert events["after_request"]["request_id"] is None
    assert events["after_request"]["parent_id"] is None


def test_bounded_buffer_reports_loss_and_unfinished_spans():
    from benchmark_analyst.diagnostic_observer import DiagnosticObserver

    observer = DiagnosticObserver(max_events=2, max_open_events=2)
    pending = observer.start_event("setup", "pending")
    for _ in range(3):
        with observer.span("setup", "bounded"):
            pass
    result = observer.drain()
    assert len(result["events"]) == 2
    assert result["metadata"]["dropped"] == 1
    assert result["metadata"]["truncated"] == 1
    assert result["metadata"]["unfinished"] == 1
    observer.end_event(pending, outcome="incomplete")
    assert observer.drain()["events"][0]["outcome"] == "incomplete"


async def test_lifespan_owns_gc_and_lag_hooks_and_restores_them_on_failure():
    from benchmark_analyst.diagnostic_observer import DiagnosticObserver

    observer = DiagnosticObserver(lag_interval_seconds=0.001)
    callbacks = list(gc.callbacks)
    with pytest.raises(RuntimeError, match="shutdown failure"):
        async with observer.lifespan():
            assert len(gc.callbacks) == len(callbacks) + 1
            gc.collect(0)
            await asyncio.sleep(0.008)
            raise RuntimeError("shutdown failure")
    assert gc.callbacks == callbacks
    result = observer.drain()
    assert result["metadata"]["unfinished"] == 0
    assert any(event["category"] == "gc" and event["generation"] == 0 for event in result["events"])
    assert any(event["name"] == "event_loop_lag" and event["lag_ns"] >= 0 for event in result["events"])
    assert observer._lag_task is None


def test_compilation_accounting_measures_utf8_and_ast_bytes_without_heap_claim(monkeypatch):
    from lfx.custom import component_compilation_cache as cache

    cache.clear_component_compilation_cache()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    artifact = cache.ComponentCompilationArtifact(
        source="é",
        generation=1,
        module_template=b"ast-pickle",
        class_name="Example",
        compiled_class=compile("pass", "<test>", "exec"),
        trusted_vector_store_alias=None,
    )
    try:
        cache.get_or_build_component_artifact("é", lambda: artifact)
        accounting = cache.component_compilation_cache_accounting()
        assert accounting["entries"] == 1
        assert accounting["source_utf8_bytes"] == 2
        assert accounting["ast_pickle_bytes"] == 10
        assert accounting["limits"]["max_entries"] == 128
        assert accounting["limits"]["max_source_bytes_per_entry"] == 262_144
        assert accounting["heap_bytes"] is None
        assert "é" not in json.dumps(accounting)
    finally:
        cache.clear_component_compilation_cache()


async def test_warm_accounting_tracks_resident_and_inflight_payloads(monkeypatch):
    from langflow.services.warm_registry.service import WarmGraphRegistry

    registry = WarmGraphRegistry(max_entries=3, max_flow_bytes=100, max_total_bytes=300)
    monkeypatch.setattr(registry, "_build", lambda *args: SimpleNamespace())
    await registry.add("one", "One", {"answer": 1}, "2026")
    async with registry._build_reservation("two", 19):
        accounting = registry.accounting_snapshot()
        assert accounting["entries"] == 1
        assert accounting["resident_payload_bytes"] == 12
        assert accounting["reservations"] == 1
        assert accounting["reserved_payload_bytes"] == 19
        assert accounting["limits"] == {"max_entries": 3, "max_flow_bytes": 100, "max_total_payload_bytes": 300}
        assert accounting["heap_bytes"] is None
    assert registry.accounting_snapshot()["reserved_payload_bytes"] == 0


async def test_real_alias_hooks_time_flow_fetch_and_class_preparation_without_mutating_result(monkeypatch):
    from benchmark_analyst.diagnostic_observer import DiagnosticObserver
    from langflow.api.v1 import endpoints
    from lfx.custom import validate

    fetch_args = []
    sentinel = object()

    async def fetch(flow_id, user_id, *, widen_for_shares=False):
        fetch_args.append((flow_id, user_id, widen_for_shares))
        return sentinel

    monkeypatch.setattr(endpoints, "get_flow_by_id_or_endpoint_name", fetch)
    observer = DiagnosticObserver()
    with observer.instrumentation(), observer.request_context("request-one"):
        assert await endpoints.get_flow_for_api_key_user("flow-one", SimpleNamespace(id="user-one")) is sentinel
        cls = validate.create_class_from_code(
            "from lfx.custom import Component\nclass Example(Component):\n    answer = 42\n"
        )
        assert cls().answer == 42
    result = observer.drain()
    assert fetch_args == [("flow-one", "user-one", True)]
    assert {event["name"] for event in result["events"]} >= {"flow_fetch", "class_preparation"}
    assert result["metadata"]["capabilities"]["setup"] is True
    assert endpoints.get_flow_by_id_or_endpoint_name is fetch


def test_setup_detail_restores_classmethod_descriptor_and_records_nested_class_work():
    import inspect

    from benchmark_analyst.diagnostic_observer import DiagnosticObserver
    from lfx.custom import validate
    from lfx.graph.graph.base import Graph

    before = inspect.getattr_static(Graph, "from_payload")
    observer = DiagnosticObserver(detail="setup")
    with pytest.raises(RuntimeError, match="private-source"), observer.instrumentation():
        with observer.request_context("request-one"):
            cls = validate.create_class_from_code(
                "from lfx.custom import Component\nclass Example(Component):\n    answer = 42\n"
            )
            assert cls().answer == 42
        raise RuntimeError("private-source")
    assert inspect.getattr_static(Graph, "from_payload") is before
    drained = observer.drain()
    names = {event["name"] for event in drained["events"]}
    assert names >= {
        "class_preparation",
        "artifact_lookup",
        "module_preparation",
        "class_exec",
        "annotation_registration",
    }
    assert "private-source" not in json.dumps(drained)


async def test_lifespan_cancellation_restores_callbacks_and_awaits_timer():
    from benchmark_analyst.diagnostic_observer import DiagnosticObserver

    before = list(gc.callbacks)
    observer = DiagnosticObserver()
    entered = asyncio.Event()

    async def running():
        async with observer.lifespan():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(running())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gc.callbacks == before
    assert observer._lag_task is None


def test_inherited_context_after_final_response_does_not_attach_gc_to_closed_request():
    from benchmark_analyst.diagnostic_observer import DiagnosticObserver

    observer = DiagnosticObserver()
    state = SimpleNamespace(finished=False)
    with observer.request_context("request-one", parent_id="closed-parent", request_state=state):
        state.finished = True
        observer._gc_callback("start", {"generation": 0})
        observer._gc_callback("stop", {"generation": 0, "collected": 3, "uncollectable": 0})
    event = observer.drain()["events"][0]
    assert event["request_id"] is None
    assert event["parent_id"] is None
