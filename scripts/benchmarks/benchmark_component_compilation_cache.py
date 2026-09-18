"""Reproducible micro/flow benchmark for custom-component compilation.

The harness intentionally uses only local pass-through components. It can run
against the pre-cache baseline as well as the cache implementation because all
cache-specific imports are optional.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import cProfile
import gc
import json
import os
import platform
import pstats
import resource
import statistics
import sys
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from copy import deepcopy
from itertools import pairwise
from pathlib import Path
from typing import Any, Self

REPO_ROOT = Path(__file__).resolve().parents[2]
for source_root in (REPO_ROOT / "src" / "lfx" / "src", REPO_ROOT / "src" / "backend" / "base"):
    sys.path.insert(0, str(source_root))

DEFAULT_SAMPLES = 40
GRAPH_SAMPLES = 7
WARMUP_ITERATIONS = 5
CONCURRENT_REQUESTS = 32
CONCURRENT_WORKERS = 8
COMPILE_FLAGS_POSITION = 3

PASS_THROUGH_SOURCE = """
from lfx.custom import Component
from lfx.io import DataInput, Output
from lfx.schema import Data


class BenchmarkPassThroughComponent(Component):
    display_name = "Benchmark Pass Through"
    inputs = [DataInput(name="value", display_name="Value", required=False)]
    outputs = [Output(name="output", display_name="Output", method="run")]

    def run(self) -> Data:
        if isinstance(self.value, Data):
            return self.value
        return Data(data={"value": self.value})
""".strip()


def _percentile(values: list[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _latency_summary(values_ns: list[int]) -> dict[str, float | int]:
    return {
        "samples": len(values_ns),
        "p50_ms": round(_percentile(values_ns, 0.50) / 1_000_000, 6),
        "p95_ms": round(_percentile(values_ns, 0.95) / 1_000_000, 6),
        "mean_ms": round(statistics.fmean(values_ns) / 1_000_000, 6),
    }


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


class ParseCompileProbe(AbstractContextManager["ParseCompileProbe"]):
    """Count and time source parsing/compilation during a measured section."""

    def __init__(self) -> None:
        self.parse_calls = 0
        self.compile_calls = 0
        self.parse_ns = 0
        self.compile_ns = 0
        self._lock = threading.Lock()
        self._original_parse = None
        self._original_compile = None

    def __enter__(self) -> Self:
        import ast

        self._original_parse = ast.parse
        self._original_compile = builtins.compile

        def timed_parse(*args, **kwargs):
            started = time.perf_counter_ns()
            try:
                return self._original_parse(*args, **kwargs)
            finally:
                elapsed = time.perf_counter_ns() - started
                with self._lock:
                    self.parse_calls += 1
                    self.parse_ns += elapsed

        def timed_compile(*args, **kwargs):
            import ast

            started = time.perf_counter_ns()
            try:
                return self._original_compile(*args, **kwargs)
            finally:
                flags = kwargs.get(
                    "flags",
                    args[COMPILE_FLAGS_POSITION] if len(args) > COMPILE_FLAGS_POSITION else 0,
                )
                if not flags & ast.PyCF_ONLY_AST:
                    elapsed = time.perf_counter_ns() - started
                    with self._lock:
                        self.compile_calls += 1
                        self.compile_ns += elapsed

        ast.parse = timed_parse
        builtins.compile = timed_compile
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        import ast

        ast.parse = self._original_parse
        builtins.compile = self._original_compile


def _cache_api() -> tuple[Any, Any] | None:
    try:
        from lfx.custom.component_compilation_cache import (
            clear_component_compilation_cache,
            component_compilation_cache_stats,
        )
    except ImportError:
        return None
    return clear_component_compilation_cache, component_compilation_cache_stats


def _clear_cache() -> None:
    api = _cache_api()
    if api is not None:
        api[0]()


def _cache_stats() -> dict[str, int] | None:
    api = _cache_api()
    if api is None:
        return None
    stats = api[1]()
    return dict(stats) if not isinstance(stats, dict) else stats.copy()


def _source_variant(index: int, *, class_name: str = "BenchmarkPassThroughComponent") -> str:
    return PASS_THROUGH_SOURCE.replace("BenchmarkPassThroughComponent", class_name).replace(
        'display_name = "Benchmark Pass Through"',
        f'display_name = "Benchmark Pass Through {index}"',
    )


def _measure_eval(source: str) -> tuple[int, int, int, int, type]:
    from lfx.custom.eval import eval_custom_component_code

    with ParseCompileProbe() as probe:
        started = time.perf_counter_ns()
        component_class = eval_custom_component_code(source)
        class_elapsed = time.perf_counter_ns() - started
    constructor_started = time.perf_counter_ns()
    component_class(_parameters={"value": f"value-{class_elapsed}"}, _id="benchmark")
    constructor_elapsed = time.perf_counter_ns() - constructor_started
    return (
        probe.parse_ns + probe.compile_ns,
        class_elapsed,
        constructor_elapsed,
        probe.parse_calls + probe.compile_calls,
        component_class,
    )


def _measure_eval_workloads() -> dict[str, Any]:
    from lfx.custom.eval import eval_custom_component_code

    _clear_cache()
    cold_preparation, cold_class, cold_constructor, cold_calls, _ = _measure_eval(PASS_THROUGH_SOURCE)

    _clear_cache()
    miss_preparation, miss_class, miss_constructor, miss_calls, _ = _measure_eval(PASS_THROUGH_SOURCE)

    for _ in range(WARMUP_ITERATIONS):
        eval_custom_component_code(PASS_THROUGH_SOURCE)

    preparation_samples: list[int] = []
    class_samples: list[int] = []
    constructor_samples: list[int] = []
    parse_compile_calls = 0
    component_classes: list[type] = []
    for _ in range(DEFAULT_SAMPLES):
        preparation, class_elapsed, constructor, calls, component_class = _measure_eval(PASS_THROUGH_SOURCE)
        preparation_samples.append(preparation)
        class_samples.append(class_elapsed)
        constructor_samples.append(constructor)
        parse_compile_calls += calls
        component_classes.append(component_class)

    distinct_samples: list[int] = []
    distinct_calls = 0
    for index in range(DEFAULT_SAMPLES):
        preparation, class_elapsed, _constructor, calls, _component_class = _measure_eval(
            _source_variant(index, class_name=f"BenchmarkPassThroughComponent{index}")
        )
        distinct_samples.append(class_elapsed)
        distinct_calls += calls

    updated_source = _source_variant(999)
    update_started = time.perf_counter_ns()
    eval_custom_component_code(updated_source)
    source_update_ns = time.perf_counter_ns() - update_started

    return {
        "cold_process_first_eval": {
            "source_preparation_ms": round(cold_preparation / 1_000_000, 6),
            "class_creation_ms": round(cold_class / 1_000_000, 6),
            "constructor_ms": round(cold_constructor / 1_000_000, 6),
            "parse_compile_calls": cold_calls,
        },
        "first_cache_miss": {
            "source_preparation_ms": round(miss_preparation / 1_000_000, 6),
            "class_creation_ms": round(miss_class / 1_000_000, 6),
            "constructor_ms": round(miss_constructor / 1_000_000, 6),
            "parse_compile_calls": miss_calls,
        },
        "steady_same_source": {
            "source_preparation": _latency_summary(preparation_samples),
            "class_creation": _latency_summary(class_samples),
            "constructor": _latency_summary(constructor_samples),
            "parse_compile_calls": parse_compile_calls,
            "fresh_class_identities": len({id(component_class) for component_class in component_classes}),
        },
        "distinct_sources": {
            "class_creation": _latency_summary(distinct_samples),
            "parse_compile_calls": distinct_calls,
        },
        "source_update_ms": round(source_update_ns / 1_000_000, 6),
    }


def _build_graph_payload(node_count: int) -> dict[str, Any]:
    from lfx.custom.eval import eval_custom_component_code
    from lfx.graph import Graph
    from lfx.schema import Data

    component_class = eval_custom_component_code(PASS_THROUGH_SOURCE)
    nodes = [component_class(_id=f"pass-{index}") for index in range(node_count)]
    nodes[0].set(value=Data(data={"value": "seed"}))
    for previous, current in pairwise(nodes):
        current.set(value=previous.run)
    payload = Graph(nodes[0], nodes[-1]).dump()["data"]
    # Dynamically created classes inherit validate.py's module metadata, so the
    # generic dump path cannot recover their original source. Real saved flows
    # carry the submitted code in this exact template field; restore it here.
    for node in payload["nodes"]:
        node["data"]["node"]["template"]["code"]["value"] = PASS_THROUGH_SOURCE
    return payload


async def _consume_graph(graph) -> None:
    async for _result in graph.async_start():
        pass


def _measure_graph_size(node_count: int) -> dict[str, Any]:
    from lfx.graph import Graph

    payload = _build_graph_payload(node_count)
    _clear_cache()
    for _ in range(2):
        graph = Graph.from_payload(deepcopy(payload))
        asyncio.run(_consume_graph(graph))

    cold_prepare: list[int] = []
    cold_flow: list[int] = []
    for _ in range(GRAPH_SAMPLES):
        started = time.perf_counter_ns()
        graph = Graph.from_payload(deepcopy(payload))
        prepared = time.perf_counter_ns()
        asyncio.run(_consume_graph(graph))
        finished = time.perf_counter_ns()
        cold_prepare.append(prepared - started)
        cold_flow.append(finished - started)

    template = Graph.from_payload(deepcopy(payload), instantiate_components=False)
    warm_prepare: list[int] = []
    warm_flow: list[int] = []
    for _ in range(GRAPH_SAMPLES):
        started = time.perf_counter_ns()
        graph = template.copy_for_run(user_id="benchmark-user")
        prepared = time.perf_counter_ns()
        asyncio.run(_consume_graph(graph))
        finished = time.perf_counter_ns()
        warm_prepare.append(prepared - started)
        warm_flow.append(finished - started)

    return {
        "nodes": node_count,
        "cold_graph_preparation": _latency_summary(cold_prepare),
        "cold_flow_wall": _latency_summary(cold_flow),
        "warm_graph_preparation": _latency_summary(warm_prepare),
        "warm_flow_wall": _latency_summary(warm_flow),
    }


def _measure_concurrent() -> dict[str, Any]:
    from lfx.custom.eval import eval_custom_component_code

    _clear_cache()
    started = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as pool:
        classes = list(pool.map(eval_custom_component_code, [PASS_THROUGH_SOURCE] * CONCURRENT_REQUESTS))
    elapsed = time.perf_counter_ns() - started
    return {
        "requests": CONCURRENT_REQUESTS,
        "workers": CONCURRENT_WORKERS,
        "wall_ms": round(elapsed / 1_000_000, 6),
        "fresh_class_identities": len({id(component_class) for component_class in classes}),
    }


def _measure_memory() -> dict[str, Any]:
    from lfx.custom.eval import eval_custom_component_code

    _clear_cache()
    gc.collect()
    before = _rss_bytes()
    tracemalloc.start()
    allocated_before, _peak_before = tracemalloc.get_traced_memory()
    for index in range(128):
        eval_custom_component_code(_source_variant(index, class_name=f"MemoryComponent{index}"))
    allocated_after, allocated_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    after = _rss_bytes()
    return {
        "rss_before_bytes": before,
        "rss_after_bytes": after,
        "rss_delta_bytes": max(0, after - before),
        "traced_allocated_delta_bytes": max(0, allocated_after - allocated_before),
        "traced_peak_bytes": allocated_peak,
        "sources": 128,
    }


def _profile_same_source() -> dict[str, Any]:
    from lfx.custom.eval import eval_custom_component_code

    _clear_cache()
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(DEFAULT_SAMPLES):
        component_class = eval_custom_component_code(PASS_THROUGH_SOURCE)
        component_class(_parameters={"value": "profile"}, _id="profile")
    profiler.disable()
    stats = pstats.Stats(profiler)
    rows = []
    for (filename, line, function), values in sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[
        :12
    ]:
        primitive_calls, total_calls, total_time, cumulative_time, _callers = values
        rows.append(
            {
                "function": f"{Path(filename).name}:{line}:{function}",
                "primitive_calls": primitive_calls,
                "total_calls": total_calls,
                "total_time_ms": round(total_time * 1000, 6),
                "cumulative_time_ms": round(cumulative_time * 1000, 6),
            }
        )
    return {"iterations": DEFAULT_SAMPLES, "top_cumulative": rows}


def _configure_mode(mode: str) -> None:
    if mode == "on":
        os.environ["LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED"] = "true"
    else:
        os.environ["LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED"] = "false"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("baseline", "off", "on"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _configure_mode(args.mode)

    # Imports occur inside workload functions so the feature flag is set before
    # SettingsService is initialized. Import/startup time is not measured.
    results = {
        "metadata": {
            "mode": args.mode,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pid": os.getpid(),
            "worker_count": 1,
            "warmup_iterations": WARMUP_ITERATIONS,
            "latency_samples": DEFAULT_SAMPLES,
            "graph_samples": GRAPH_SAMPLES,
            "concurrent_workers": CONCURRENT_WORKERS,
            "concurrent_requests": CONCURRENT_REQUESTS,
        },
        "evaluation": _measure_eval_workloads(),
        "memory": _measure_memory(),
        "graphs": {
            "10_nodes": _measure_graph_size(10),
            "100_nodes": _measure_graph_size(100),
        },
        "concurrent_same_source": _measure_concurrent(),
        "cpu_profile": _profile_same_source(),
        "cache_stats": _cache_stats(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
