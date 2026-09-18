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
import shutil
import statistics
import subprocess
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
GRAPH_SAMPLES = 15
WARMUP_ITERATIONS = 5
CONCURRENT_REQUESTS = 32
CONCURRENT_WORKERS = 8
CONCURRENT_BATCH_SAMPLES = 15
COMPILE_FLAGS_POSITION = 3
BASELINE_SHA = "c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d"  # pragma: allowlist secret

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
    if not values_ns:
        return {"samples": 0, "p50_ms": 0.0, "p95_ms": 0.0, "mean_ms": 0.0}
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
    """Count and time all AST parsing/compilation during a measured section."""

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


class ArtifactPhaseProbe(AbstractContextManager["ArtifactPhaseProbe"]):
    """Time the implementation's source-artifact build and AST restore phases."""

    def __init__(self) -> None:
        self.build_ns: list[int] = []
        self.restore_ns: list[int] = []
        self._validate_module = None
        self._original_build = None
        self._original_restore = None

    def __enter__(self) -> Self:
        from lfx.custom import validate

        self._validate_module = validate
        self._original_build = getattr(validate, "_prepare_component_compilation_artifact", None)
        self._original_restore = getattr(validate, "_load_component_module_template", None)
        if self._original_build is not None:

            def timed_build(*args, **kwargs):
                started = time.perf_counter_ns()
                try:
                    return self._original_build(*args, **kwargs)
                finally:
                    self.build_ns.append(time.perf_counter_ns() - started)

            validate._prepare_component_compilation_artifact = timed_build  # noqa: SLF001
        if self._original_restore is not None:

            def timed_restore(*args, **kwargs):
                started = time.perf_counter_ns()
                try:
                    return self._original_restore(*args, **kwargs)
                finally:
                    self.restore_ns.append(time.perf_counter_ns() - started)

            validate._load_component_module_template = timed_restore  # noqa: SLF001
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._original_build is not None:
            self._validate_module._prepare_component_compilation_artifact = self._original_build  # noqa: SLF001
        if self._original_restore is not None:
            self._validate_module._load_component_module_template = self._original_restore  # noqa: SLF001


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
    steady_cache_stats = _cache_stats()

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
            "parse_compile_activity_ms": round(cold_preparation / 1_000_000, 6),
            "class_creation_ms": round(cold_class / 1_000_000, 6),
            "constructor_ms": round(cold_constructor / 1_000_000, 6),
            "parse_compile_calls": cold_calls,
        },
        "first_cache_miss": {
            "parse_compile_activity_ms": round(miss_preparation / 1_000_000, 6),
            "class_creation_ms": round(miss_class / 1_000_000, 6),
            "constructor_ms": round(miss_constructor / 1_000_000, 6),
            "parse_compile_calls": miss_calls,
        },
        "steady_same_source": {
            "parse_compile_activity": _latency_summary(preparation_samples),
            "class_creation": _latency_summary(class_samples),
            "constructor": _latency_summary(constructor_samples),
            "parse_compile_calls": parse_compile_calls,
            "fresh_class_identities": len({id(component_class) for component_class in component_classes}),
            "cache_stats": steady_cache_stats,
        },
        "distinct_sources": {
            "class_creation": _latency_summary(distinct_samples),
            "parse_compile_calls": distinct_calls,
        },
        "source_update_ms": round(source_update_ns / 1_000_000, 6),
    }


def _measure_artifact_phases() -> dict[str, Any]:
    from lfx.custom.eval import eval_custom_component_code

    _clear_cache()
    with ArtifactPhaseProbe() as probe:
        eval_custom_component_code(PASS_THROUGH_SOURCE)
        for _ in range(DEFAULT_SAMPLES):
            eval_custom_component_code(PASS_THROUGH_SOURCE)
    return {
        "artifact_build": _latency_summary(probe.build_ns),
        "cache_hit_ast_restore": _latency_summary(probe.restore_ns),
        "build_calls": len(probe.build_ns),
        "restore_calls": len(probe.restore_ns),
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
    warm_prepare: list[int] = []
    warm_flow: list[int] = []
    template = Graph.from_payload(deepcopy(payload), instantiate_components=False)
    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        for _ in range(GRAPH_SAMPLES):
            started = time.perf_counter_ns()
            graph = Graph.from_payload(deepcopy(payload))
            prepared = time.perf_counter_ns()
            asyncio.run(_consume_graph(graph))
            finished = time.perf_counter_ns()
            cold_prepare.append(prepared - started)
            cold_flow.append(finished - started)

        for _ in range(GRAPH_SAMPLES):
            started = time.perf_counter_ns()
            graph = template.copy_for_run(user_id="benchmark-user")
            prepared = time.perf_counter_ns()
            asyncio.run(_consume_graph(graph))
            finished = time.perf_counter_ns()
            warm_prepare.append(prepared - started)
            warm_flow.append(finished - started)
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()

    return {
        "nodes": node_count,
        "cold_graph_preparation": _latency_summary(cold_prepare),
        "cold_flow_wall": _latency_summary(cold_flow),
        "warm_graph_preparation": _latency_summary(warm_prepare),
        "warm_flow_wall": _latency_summary(warm_flow),
        "cache_stats": _cache_stats(),
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
        "cache_stats": _cache_stats(),
    }


def _measure_concurrent_distinct_sources() -> dict[str, Any]:
    from lfx.custom.eval import eval_custom_component_code

    wall_samples: list[int] = []
    fresh_class_identities = 0
    for sample in range(CONCURRENT_BATCH_SAMPLES):
        _clear_cache()
        sources = [
            _source_variant(
                sample * CONCURRENT_REQUESTS + index,
                class_name=f"ConcurrentDistinctComponent{sample}_{index}",
            )
            for index in range(CONCURRENT_REQUESTS)
        ]
        started = time.perf_counter_ns()
        with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as pool:
            classes = list(pool.map(eval_custom_component_code, sources))
        wall_samples.append(time.perf_counter_ns() - started)
        fresh_class_identities = len({id(component_class) for component_class in classes})
    return {
        "requests_per_batch": CONCURRENT_REQUESTS,
        "workers": CONCURRENT_WORKERS,
        "samples": CONCURRENT_BATCH_SAMPLES,
        "wall": _latency_summary(wall_samples),
        "fresh_class_identities_last_batch": fresh_class_identities,
        "cache_stats_last_batch": _cache_stats(),
    }


def _detected_source_revision() -> str | None:
    git_executable = shutil.which("git")
    if git_executable is None:
        return None
    completed = subprocess.run(  # noqa: S603
        [git_executable, "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() or None


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
    parser.add_argument("--source-revision", help="Commit SHA of the source tree under measurement")
    args = parser.parse_args()
    source_revision = args.source_revision or _detected_source_revision()
    if args.mode == "baseline" and source_revision != BASELINE_SHA:
        parser.error(f"baseline mode requires --source-revision {BASELINE_SHA}")
    if source_revision is None:
        parser.error("unable to detect source revision; pass --source-revision explicitly")
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
            "latency_gc": "disabled during graph timing; allocations measured separately",
            "concurrent_workers": CONCURRENT_WORKERS,
            "concurrent_requests": CONCURRENT_REQUESTS,
            "concurrent_batch_samples": CONCURRENT_BATCH_SAMPLES,
            "source_revision": source_revision[:12],
        },
        "evaluation": _measure_eval_workloads(),
        "artifact_phases": _measure_artifact_phases(),
        "memory": _measure_memory(),
        "graphs": {
            "10_nodes": _measure_graph_size(10),
            "100_nodes": _measure_graph_size(100),
        },
        "concurrent_same_source": _measure_concurrent(),
        "concurrent_distinct_sources": _measure_concurrent_distinct_sources(),
        "cpu_profile": _profile_same_source(),
        "cache_stats": _cache_stats(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
