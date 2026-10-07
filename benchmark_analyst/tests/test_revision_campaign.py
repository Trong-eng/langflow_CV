"""Contracts for the two actual-checkout comparison, without live inference."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from benchmark_analyst.campaign_contract import observed_settings
from benchmark_analyst.memory_metrics import CHECKPOINTS, resource_declaration
from benchmark_analyst.revision_campaign import (
    FIXED_PROFILE,
    PINNED_MAIN,
    check_smoke,
    digest,
    make_schedule,
    validate_run,
    verify_identity,
)
from benchmark_analyst.revision_worker import (
    HARNESS_ROOT,
    IDENTITY_MODULES,
    RevisionMiddleware,
    RevisionWorker,
    module_origins,
    optional_cache_accounting,
)
from benchmark_analyst.worker_observer import WorkerObserver


def valid_raw(directory):
    """Full strict raw fixture; timers and checkpoint ordering are hand-checkable."""
    directory.mkdir(exist_ok=True)
    groups = {}
    for group in ("MAIN", "COMPILE"):
        names = list(IDENTITY_MODULES) + (["lfx.custom.component_compilation_cache"] if group == "COMPILE" else [])
        root = directory / group
        modules = {
            name: {
                "path": str(
                    root
                    / (
                        ("src/backend/base/" if name.startswith("langflow") else "src/lfx/src/")
                        + name.replace(".", "/")
                        + ".py"
                    )
                ),
                "sha256": "a" * 64,
            }
            for name in names
        }
        files = {str(Path(value["path"]).relative_to(root)): value["sha256"] for value in modules.values()}
        groups[group] = {
            "root": str(root),
            "source": {
                "git_revision": PINNED_MAIN if group == "MAIN" else "b" * 40,
                "files": files,
                "sha256": digest(files),
                "working_tree_clean": True,
            },
            "modules": modules,
        }
    versions = {"python": "3.13.14", "requests": "2.34.2"}
    common = {
        "storage_type": "local",
        "database_backend": "sqlite",
        "database_engine": {"status": "measured", "dialect": "sqlite"},
        "product_telemetry_enabled": True,
        "native_tracing": True,
        "uvicorn_access_enabled": False,
        "gc_enabled": True,
        "versions": versions,
    }
    profile = {**FIXED_PROFILE, "effective_common_settings": observed_settings({"effective": common})}
    files = {"observer.py": "a" * 64}
    manifest = {
        "schema_version": 2,
        "kind": "revision_comparison",
        "experiment_id": directory.name,
        "groups": {k: {key: value for key, value in v.items() if key != "modules"} for k, v in groups.items()},
        "runtime": {"interpreter": "/python", "workers": 1, "concurrency": 1},
        "source": {"files": files, "sha256": digest(files)},
        "workload": {"reference_pixel_sha256": "c" * 64, "scrfd_node_ids": ["load", "detect", "draw", "save"]},
        "fixture_sha256": "d" * 64,
        "profile": profile,
        "profile_id": digest(profile),
        "per_block": 2,
        "requests_per_arm": 8,
        "mode": "smoke",
        "warmups": 5,
        "blocks": 4,
        "schedule": make_schedule(2),
        "resource_measurement": resource_declaration(),
    }
    requests, evidence, memory = [], [], []
    for slot_index, slot in enumerate(manifest["schedule"]):
        group, block = slot["arm"], slot["block"]
        base = datetime(2026, 10, 6, tzinfo=timezone.utc) + timedelta(minutes=slot_index)

        def stamp(second, _base=base):
            return (_base + timedelta(seconds=second)).isoformat()

        pid, created = 1000 + slot_index, 100.0 + slot_index
        slot_fixture = {
            "database_path": str(directory / "slots" / str(slot_index) / "database.db"),
            "config_dir": str(directory / "slots" / str(slot_index) / "storage"),
            "fixture_sha256": manifest["fixture_sha256"],
        }
        item = {**slot, "fixture_sha256": manifest["fixture_sha256"], "slot_fixture": slot_fixture}
        for checkpoint, second, count in zip(CHECKPOINTS, (0, 6, 9, 14), (0, 5, 7, 7), strict=True):
            compilation = (
                None
                if group == "MAIN"
                else {
                    "hits": max(0, count - 1) * 6,
                    "misses": 6 if count else 0,
                    "builds": 6 if count else 0,
                    "bypasses": 0,
                    "evictions": 0,
                    "entries": 6 if count else 0,
                }
            )
            snapshot = {
                "pid": pid,
                "process_create_time": created,
                "worker_count": 1,
                "settings": {
                    "component_compilation_cache_enabled": True if group == "COMPILE" else None,
                    "warm_registry_enabled": False,
                },
                "configured": {"compilation_cache": group == "COMPILE", "warm_registry": False},
                "warm": {"attempts": count, "cold": count, "hits": 0, "errors": 0},
                "registry_entries": 0,
                "compilation": compilation,
                "compilation_status": {
                    "status": "unavailable" if group == "MAIN" else "measured",
                    "reason": "absent_in_baseline" if group == "MAIN" else None,
                },
                "identity": {
                    "group": group,
                    "root": groups[group]["root"],
                    "interpreter": "/python",
                    "modules": groups[group]["modules"],
                    "module_origin_failures": [],
                    "dependency_versions": versions,
                    "dependency_sha256": digest(versions),
                },
                "effective": {**common, **slot_fixture},
                "snapshot_started_at_utc": stamp(second),
                "snapshot_duration_ms": 1.0,
            }
            item[checkpoint] = snapshot

            def metric(value, offset, status="measured", reason=None, _second=second, _stamp=stamp):
                return {
                    "bytes": value,
                    "status": status,
                    "reason": reason,
                    "started_at_utc": _stamp(_second + offset),
                    "duration_ms": 1.0,
                }

            memory.append(
                {
                    "schema_version": 1,
                    "experiment_id": directory.name,
                    "block": block,
                    "arm": group,
                    "checkpoint": checkpoint,
                    "pid": pid,
                    "process_create_time": created,
                    "elapsed_since_last_sample_ms": None
                    if checkpoint == "before_warmup"
                    else (5500.0 if checkpoint == "after_idle" else 500.0),
                    "started_at_utc": stamp(second + 0.01),
                    "finished_at_utc": stamp(second + 0.05),
                    "collector": {"name": "psutil", "version": "7.2.2"},
                    "rss": metric(100000000, 0.02),
                    "uss": metric(None, 0.03, "unavailable", "AccessDenied"),
                    "cache_accounting": {
                        "started_at_utc": stamp(second),
                        "duration_ms": 1.0,
                        "snapshot": {"compilation": None},
                    },
                }
            )
        item["compilation_delta"] = (
            None if group == "MAIN" else {"hits": 12, "misses": 0, "builds": 0, "bypasses": 0, "evictions": 0}
        )
        evidence.append(item)
        for phase, count, offset in (("warmup", 5, 1), ("measured", 2, 7)):
            for index in range(count):
                requests.append(
                    {
                        "block": block,
                        "arm": group,
                        "phase": phase,
                        "index": index,
                        "request_id": str(uuid4()),
                        "pid": pid,
                        "outcome": "success",
                        "output_valid": True,
                        "http_status": 200,
                        "warm_path": "cold",
                        "image_pixel_sha256": "c" * 64,
                        "output_image_path": str(uuid4()),
                        "server_total_ms": 10.0,
                        "scrfd_processing_ms": 4.0,
                        "langflow_overhead_ms": 6.0,
                        "flow_api_ms": 11.0,
                        "upload_ms": 1.0,
                        "started_at": stamp(offset + index),
                        "finished_at": stamp(offset + index + 0.5),
                        "component_intervals_ms": [
                            {"node_id": name, "start_ms": i + 1.0, "end_ms": i + 2.0}
                            for i, name in enumerate(manifest["workload"]["scrfd_node_ids"])
                        ],
                    }
                )
    for name, value in (("manifest.json", manifest), ("state.json", {"status": "COMPLETE"})):
        (directory / name).write_text(json.dumps(value))
    for name, values in (("requests.jsonl", requests), ("worker_evidence.jsonl", evidence), ("memory.jsonl", memory)):
        (directory / name).write_text("".join(json.dumps(value) + "\n" for value in values))
    return manifest, requests, evidence, memory


def test_balanced_schedule_is_the_prespecified_eight_workers():
    assert [(s["block"], s["arm"], s["count"]) for s in make_schedule(250)] == [
        (1, "MAIN", 250),
        (1, "COMPILE", 250),
        (2, "COMPILE", 250),
        (2, "MAIN", 250),
        (3, "COMPILE", 250),
        (3, "MAIN", 250),
        (4, "MAIN", 250),
        (4, "COMPILE", 250),
    ]


def test_worker_environment_has_only_selected_sources_and_shared_harness(tmp_path):
    root = tmp_path / "chosen"
    worker = RevisionWorker(root, 7876, env_file=None, credentials_file=None, group="MAIN")
    env = worker.environment("MAIN", tmp_path / "scratch")
    paths = env["PYTHONPATH"].split(":")
    assert paths[1:] == [str(root / "src/backend/base"), str(root / "src/lfx/src"), str(root / "src/sdk/src")]
    assert env["LANGFLOW_WARM_REGISTRY_ENABLED"] == "false"
    assert env["LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED"] == "false"
    assert env["LANGFLOW_BENCHMARK_REVISION_GROUP"] == "MAIN"


def test_identity_rejects_a_feature_checkout_import_and_changed_bytes(tmp_path):
    manifest, _, evidence, _ = valid_raw(tmp_path)
    snapshot = evidence[0]["before_warmup"]
    verify_identity(snapshot, "MAIN", manifest)
    wrong = copy.deepcopy(snapshot)
    wrong["identity"]["modules"]["lfx"]["path"] = str(tmp_path / "feature" / "lfx.py")
    with pytest.raises(ValueError, match="imported outside"):
        verify_identity(wrong, "MAIN", manifest)
    wrong = copy.deepcopy(snapshot)
    wrong["identity"]["modules"]["lfx"]["sha256"] = "changed"
    with pytest.raises(ValueError, match="source hash"):
        verify_identity(wrong, "MAIN", manifest)


def test_smoke_requires_each_group_source_and_raw_integrity(tmp_path):
    manifest, _, _, _ = valid_raw(tmp_path)
    result = validate_run(tmp_path)
    assert result["overall_valid"], result["errors"]
    (tmp_path / "validation.json").write_text(json.dumps(result))
    check_smoke(tmp_path, manifest)
    wrong = copy.deepcopy(manifest)
    wrong["groups"]["MAIN"]["source"]["sha256"] = "different"
    with pytest.raises(ValueError, match="MAIN source"):
        check_smoke(tmp_path, wrong)
    (tmp_path / "requests.jsonl").write_text("changed\n")
    with pytest.raises(ValueError, match="integrity"):
        check_smoke(tmp_path, manifest)


@pytest.mark.parametrize(
    "mutation", ["source", "critical", "counters", "probe", "fixture", "profile", "count", "dirty", "origin", "output"]
)
def test_offline_validator_rejects_contract_corruption(tmp_path, mutation):
    manifest, requests, evidence, memory = valid_raw(tmp_path)
    assert validate_run(tmp_path)["overall_valid"]
    if mutation == "source":
        manifest["groups"]["MAIN"]["source"]["git_revision"] = "e" * 40
    elif mutation == "critical":
        evidence[0]["before_warmup"]["identity"]["modules"].pop("lfx.custom.validate")
    elif mutation == "counters":
        evidence[1]["after_measurement"]["compilation"]["misses"] += 1
    elif mutation == "probe":
        evidence[0]["after_warmup"]["snapshot_started_at_utc"] = requests[0]["started_at"]
    elif mutation == "fixture":
        evidence[0]["fixture_sha256"] = "bad"
    elif mutation == "profile":
        manifest["profile"]["warm_graph"] = True
        manifest["profile_id"] = digest(manifest["profile"])
    elif mutation == "count":
        manifest["per_block"] = 3
    elif mutation == "dirty":
        manifest["groups"]["MAIN"]["source"]["working_tree_clean"] = False
    elif mutation == "origin":
        evidence[0]["before_warmup"]["identity"]["module_origin_failures"] = [{"module": "lfx.custom.validate"}]
    elif mutation == "output":
        requests[0]["output_valid"] = False
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for name, values in (("requests.jsonl", requests), ("worker_evidence.jsonl", evidence), ("memory.jsonl", memory)):
        (tmp_path / name).write_text("".join(json.dumps(value) + "\n" for value in values))
    assert not validate_run(tmp_path)["overall_valid"]


def test_origin_observation_preserves_real_lazy_compatibility_aliases(tmp_path):
    from langflow import LangflowCompatibilityModule

    lazy = LangflowCompatibilityModule("langflow.unobserved", "nonexistent_optional_dependency_for_test")
    alias = ModuleType("langflow.schema.data")
    alias.__file__ = str(tmp_path / "src/lfx/src/lfx/schema/data.py")
    sdk = ModuleType("langflow_sdk")
    sdk.__file__ = str(tmp_path / "src/sdk/src/langflow_sdk/__init__.py")
    origins, failures = module_origins(
        tmp_path, {"langflow.unobserved": lazy, alias.__name__: alias, sdk.__name__: sdk}
    )
    assert "langflow.unobserved" not in origins
    assert lazy.__dict__["_lfx_module"] is None
    assert not failures
    alias.__file__ = str(tmp_path / "other_checkout/lfx/schema/data.py")
    assert module_origins(tmp_path, {alias.__name__: alias})[1]


@pytest.mark.asyncio
async def test_external_clock_finishes_after_body_send_before_background_work(monkeypatch):
    clock = [0]
    observer = WorkerObserver(clock=lambda: clock[0])
    monkeypatch.setattr(observer, "instrumentation", nullcontext)
    monkeypatch.setenv("LANGFLOW_BENCHMARK_WORKER_COUNT", "1")
    request_id = str(uuid4())

    async def app(scope, receive, send):
        clock[0] = 10_000_000
        await send({"type": "http.response.start", "status": 200, "headers": []})
        clock[0] = 20_000_000
        await send({"type": "http.response.body", "body": b"response"})
        clock[0] = 100_000_000

    sent = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body":
            clock[0] += 5_000_000

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/run/flow",
        "headers": [(b"x-langflow-benchmark-request-id", request_id.encode())],
    }
    await RevisionMiddleware(app, observer=observer)(scope, receive, send)
    record = observer.consume(request_id)
    assert record["server_total_ms"] == 25.0
    assert record["complete"]
    assert dict(sent[0]["headers"])[b"x-langflow-benchmark-request-id"] == request_id.encode()


def test_inherited_cleanup_stops_only_the_owned_process_and_scratch(tmp_path):
    import psutil

    worker = RevisionWorker(tmp_path, 7876, env_file=None, credentials_file=None, group="MAIN")
    worker.scratch = tempfile.TemporaryDirectory(dir=tmp_path)
    scratch = Path(worker.scratch.name)
    worker.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    process = worker.process
    worker._owned_group_id = process.pid
    worker._owned_processes = {process.pid: psutil.Process(process.pid).create_time()}
    worker.stop()
    assert process.poll() is not None
    assert worker.process is None and not scratch.exists()


def test_actual_minimal_cache_module_has_no_required_accounting_interface(tmp_path):
    path = tmp_path / "minimal_cache.py"
    path.write_bytes(
        subprocess.check_output(
            [
                "git",
                "show",
                "c20b6591292e8c3e979db59f831987f3bc4cfbf0:src/lfx/src/lfx/custom/component_compilation_cache.py",
            ],
            cwd=HARNESS_ROOT,
        )
    )
    spec = importlib.util.spec_from_file_location("minimal_cache_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert module.component_compilation_cache_stats()["entries"] == 0
        assert getattr(module, "component_compilation_cache_accounting", None) is None
        assert optional_cache_accounting(module) is None
    finally:
        sys.modules.pop(spec.name)
