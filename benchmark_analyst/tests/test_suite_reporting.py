"""Diagnostic suites pair independently valid child runs by their outer blocks."""
# ruff: noqa: S101, PLR2004, INP001

import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from benchmark_analyst import reporting
from benchmark_analyst.campaign_contract import diagnostics_declaration, observed_settings, profile_identity
from benchmark_analyst.memory_metrics import resource_declaration

VARIANTS = ("D0", "D1", "D2", "D3")
PHASES = ("before_warmup", "after_warmup", "after_measurement", "after_idle")
PRIMARY = "langflow_overhead_ms"


def suite_module():
    return importlib.import_module("benchmark_analyst.suite_reporting")


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def write_lines(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def global_schedule():
    rows = []
    for index in range(4):
        variants = VARIANTS[index:] + VARIANTS[:index]
        arms = ("10", "11") if index in (0, 3) else ("11", "10")
        for variant in variants:
            rows.extend(
                {"block": index + 1, "outer_block_id": index + 1, "arm": arm, "variant": variant, "count": 2}
                for arm in arms
            )
    return rows


@pytest.fixture
def diagnostic_suite(tmp_path, monkeypatch):
    # Drawing PNGs is outside the validation/comparison boundary under test.
    monkeypatch.setattr(reporting, "_charts", lambda *_: None)
    for name in ("_diagnostic_charts", "_tradeoff_charts", "_additional_charts", "_extra_charts"):
        if hasattr(reporting, name):
            monkeypatch.setattr(reporting, name, lambda *_: None)
    workload = {
        "scrfd_node_ids": ["load", "detect", "draw", "save"],
        "reference_pixel_sha256": "a" * 64,
        "flow_sha256": "b" * 64,
        "input_sha256": "c" * 64,
        "model_sha256": "d" * 64,
    }
    source = {"sha256": "e" * 64, "git_head": "f" * 40, "file_sha256": {"observer.py": "f" * 64}}
    suite = {
        "schema_version": 1,
        "kind": "diagnostic_suite",
        "status": "COMPLETE",
        "mode": "smoke",
        "blocks": 4,
        "requests_per_arm": 8,
        "schedule": global_schedule(),
        "children": {variant: variant for variant in VARIANTS},
        "source": source,
        "workload": workload,
        "fixture_sha256": "1" * 64,
        "suite_events": {"version": 1, "enabled": True},
    }
    write_json(tmp_path / "suite.json", suite)
    events = []
    epoch = datetime(2026, 10, 2, tzinfo=timezone.utc)
    for sequence, slot in enumerate(suite["schedule"], start=1):
        events.append(
            {
                "sequence": sequence,
                **slot,
                "started_at_utc": (epoch + timedelta(seconds=sequence * 120)).isoformat(),
                "finished_at_utc": (epoch + timedelta(seconds=sequence * 120 + 60)).isoformat(),
            }
        )
    write_lines(tmp_path / "suite_events.jsonl", events)
    for variant_index, variant in enumerate(VARIANTS):
        directory = tmp_path / variant
        directory.mkdir()
        warmups = 20 if variant == "D3" else 5
        effective = {
            "native_tracing": True,
            "gc_enabled": True,
            "product_telemetry_enabled": variant != "D2",
            "uvicorn_access_enabled": False,
        }
        profile_id, profile = profile_identity(
            "1" * 64,
            diagnostics=variant != "D0",
            telemetry=variant != "D2",
            warmups=warmups,
            effective_common_settings=observed_settings({"effective": effective}),
        )
        slots = [slot for slot in suite["schedule"] if slot["variant"] == variant]
        manifest = {
            "schema_version": 1,
            "kind": "diagnostic",
            "variant": variant,
            "mode": "smoke",
            "experiment_id": f"suite-{variant}",
            "blocks": 4,
            "requests_per_arm": 8,
            "warmups": warmups,
            "primary_metric": PRIMARY,
            "schedule": slots,
            "source": source,
            "workload": workload,
            "fixture_sha256": "1" * 64,
            "profile": profile,
            "profile_id": profile_id,
            "diagnostics": diagnostics_declaration(variant != "D0"),
            "resource_measurement": resource_declaration(),
        }
        rows, evidence, memory, diagnostic_events, metadata = [], [], [], [], []
        for slot_index, slot in enumerate(slots):
            block, arm = slot["block"], slot["arm"]
            event = next(
                event for event in events if all(event[key] == slot[key] for key in ("block", "arm", "variant"))
            )
            slot_start = datetime.fromisoformat(event["started_at_utc"])
            pid = 1000 + variant_index * 100 + slot_index
            worker = {"block": block, "arm": arm}
            worker_events = []
            delta = {"D0": 0, "D1": 3, "D2": -2, "D3": 1}[variant]
            for phase, count in (("warmup", warmups), ("measured", 2)):
                for index in range(count):
                    overhead = 50 + block + (10 if arm == "10" else 0) + delta + 2 * index
                    request_id = f"{variant}-{block}-{arm}-{phase}-{index}"
                    sample_index = index + (warmups if phase == "measured" else 0)
                    request_start = slot_start + timedelta(seconds=10 + sample_index / 10)
                    spans = [
                        {"node_id": name, "start_ms": 10 * i, "end_ms": 10 * (i + 1)}
                        for i, name in enumerate(workload["scrfd_node_ids"])
                    ]
                    row = {
                        "block": block,
                        "outer_block_id": block,
                        "arm": arm,
                        "phase": phase,
                        "index": index,
                        "request_id": request_id,
                        "started_at": request_start.isoformat(),
                        "finished_at": (request_start + timedelta(milliseconds=50)).isoformat(),
                        "pid": pid,
                        "outcome": "success",
                        "error": None,
                        "output_valid": True,
                        PRIMARY: overhead,
                        "server_total_ms": overhead + 40,
                        "scrfd_processing_ms": 40,
                        "flow_api_ms": overhead + 43,
                        "upload_ms": 1,
                        "warm_path": "warm" if arm == "11" and phase == "measured" else "cold",
                        "image_pixel_sha256": "a" * 64,
                        "component_intervals_ms": spans,
                    }
                    rows.append(row)
                    if variant != "D0":
                        event_spans = [
                            ("request", "asgi_request", 0, overhead + 40, None),
                            ("setup", "pre_component", 0, 0, None),
                        ]
                        event_spans.extend(
                            ("component", "component_body", span["start_ms"], span["end_ms"], span["node_id"])
                            for span in spans
                        )
                        for event_index, (category, name, start, end, node) in enumerate(event_spans):
                            event = {
                                "schema_version": 1,
                                "experiment_id": manifest["experiment_id"],
                                "block": block,
                                "arm": arm,
                                "pid": pid,
                                "process_create_time": float(pid),
                                "event_id": f"{request_id}-{event_index}",
                                "request_id": request_id,
                                "parent_id": f"{request_id}-0" if event_index else None,
                                "category": category,
                                "name": name,
                                "start_ns": start * 1000000,
                                "end_ns": end * 1000000,
                                "thread_id": 1,
                                "outcome": "success",
                            }
                            if node:
                                event["node_id"] = node
                            worker_events.append(event)
            for phase_index, checkpoint in enumerate(PHASES):
                count = (0, warmups, warmups + 2, warmups + 2)[phase_index]
                snapshot = {
                    "pid": pid,
                    "process_create_time": float(pid),
                    "effective": effective,
                    "settings": {
                        "component_compilation_cache_enabled": True,
                        "warm_registry_enabled": arm == "11",
                        "http_connection_reuse_enabled": False,
                    },
                    "compilation": {"hits": count, "bypasses": 0},
                    "warm": {
                        "hits": max(0, count - 1) if arm == "11" else 0,
                        "cold": min(1, count) if arm == "11" else count,
                    },
                    "registry_entries": int(arm == "11" and count > 0),
                }
                worker[checkpoint] = snapshot
                started = slot_start + timedelta(seconds=(1, 15, 20, 26)[phase_index])
                memory.append(
                    {
                        "schema_version": 1,
                        "experiment_id": manifest["experiment_id"],
                        "block": block,
                        "arm": arm,
                        "checkpoint": checkpoint,
                        "pid": pid,
                        "process_create_time": float(pid),
                        "elapsed_since_last_sample_ms": None if phase_index < 2 else (0 if phase_index == 2 else 5000),
                        "started_at_utc": started.isoformat(),
                        "finished_at_utc": (started + timedelta(seconds=2)).isoformat(),
                        "collector": {"name": "psutil", "version": "test"},
                        "rss": {
                            "bytes": 1000000,
                            "status": "measured",
                            "reason": None,
                            "started_at_utc": started.isoformat(),
                            "duration_ms": 0.1,
                        },
                        "uss": {
                            "bytes": None,
                            "status": "unavailable",
                            "reason": "AccessDenied",
                            "started_at_utc": (started + timedelta(seconds=1)).isoformat(),
                            "duration_ms": 0,
                        },
                        "cache_accounting": {"started_at_utc": started.isoformat(), "duration_ms": 0, "snapshot": {}},
                    }
                )
            evidence.append(worker)
            diagnostic_events.extend(worker_events)
            if variant != "D0":
                metadata.append(
                    {
                        "experiment_id": manifest["experiment_id"],
                        "block": block,
                        "arm": arm,
                        "metadata": {
                            "enabled": True,
                            "detail": "coarse",
                            "clock": "worker_monotonic",
                            "unit": "ns",
                            "pid": pid,
                            "process_create_time": float(pid),
                            "drained": len(worker_events),
                            "total_drained": len(worker_events),
                            "dropped": 0,
                            "truncated": 0,
                            "unfinished": 0,
                            "capabilities": {name: True for name in ("setup", "components", "gc", "event_loop_lag")},
                        },
                    }
                )
        write_json(directory / "manifest.json", manifest)
        write_json(directory / "state.json", {"status": "COMPLETE"})
        for name, records in (
            ("requests.jsonl", rows),
            ("worker_evidence.jsonl", evidence),
            ("memory.jsonl", memory),
            ("diagnostic_events.jsonl", diagnostic_events),
            ("diagnostic_metadata.jsonl", metadata),
        ):
            write_lines(directory / name, records)
    return tmp_path


def test_complete_suite_reports_paired_policy_differences_and_preserves_raw(diagnostic_suite):
    directory = diagnostic_suite
    raw = {path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob("*.json*")}
    result = suite_module().analyze_suite(directory)
    assert result["valid"] is result["overall_valid"] is True
    assert result["errors"] == []
    assert result["mode"] == "smoke"
    assert result["is_full_campaign"] is False
    observer = result["comparisons"]["D1-D0"]
    assert observer["blocks"]["1"]["10"][PRIMARY]["mean"] == 3
    assert observer["blocks"]["1"]["10"][PRIMARY]["p95"] == pytest.approx(3)
    assert observer["arms"]["11"][PRIMARY]["p99"]["mean"] == pytest.approx(3)
    assert result["comparisons"]["D2-D1"]["blocks"]["2"]["11"][PRIMARY]["p50"] == -5
    assert result["comparisons"]["D3-D1"]["blocks"]["4"]["10"][PRIMARY]["mean"] == -2
    assert result["children"]["D0"]["diagnostic_validity"]["requested"] is False
    assert result["children"]["D0"]["arms"]["10"][PRIMARY]["n"] == 8
    assert "No CI" in result["statistical_note"]
    for name in ("suite_analysis.json", "suite_report.md", "suite_report.html"):
        assert (directory / name).stat().st_size > 0
    html = (directory / "suite_report.html").read_text()
    assert 'href="D0/report.html"' in html
    assert "warmup policy includes" in html
    for name, original in raw.items():
        assert (directory / name).read_bytes() == original
        if name.startswith("D"):
            assert (
                result["children"][name.split("/")[0]]["raw_sha256"][name.split("/")[1]]
                == hashlib.sha256(original).hexdigest()
            )


@pytest.mark.parametrize(
    "mutation",
    [
        "incomplete",
        "wrong_kind",
        "reordered_schedule",
        "wrong_child_schedule",
        "source_drift",
        "workload_drift",
        "fixture_drift",
        "duplicate_experiment",
        "wrong_variant",
        "warmup_policy",
        "profile_drift",
        "profile_hash",
        "missing_ram",
        "missing_diagnostics",
        "child_failed",
        "missing_events",
        "duplicate_event",
        "wrong_sequence",
        "wrong_event_slot",
        "path_traversal",
        "symlink_escape",
        "literal_child_symlink",
        "malformed_child",
        "bad_schema",
        "numeric_bool_profile",
        "invalid_event_declaration",
        "bool_event_declaration",
        "bool_event_block",
        "bool_outer_block",
    ],
)
def test_invalid_suite_retains_child_findings_but_omits_comparisons(diagnostic_suite, tmp_path, mutation):
    directory = diagnostic_suite
    suite = json.loads((directory / "suite.json").read_text())
    path = directory / "D2" / "manifest.json"
    child = json.loads(path.read_text())
    if mutation == "incomplete":
        suite["status"] = "RUNNING"
    elif mutation == "wrong_kind":
        suite["kind"] = "factorial"
    elif mutation == "reordered_schedule":
        suite["schedule"][0], suite["schedule"][1] = suite["schedule"][1], suite["schedule"][0]
    elif mutation == "wrong_child_schedule":
        child["schedule"][0], child["schedule"][1] = child["schedule"][1], child["schedule"][0]
    elif mutation == "source_drift":
        child["source"]["sha256"] = "0" * 64
    elif mutation == "workload_drift":
        child["workload"]["flow_sha256"] = "0" * 64
    elif mutation == "fixture_drift":
        child["fixture_sha256"] = "0" * 64
    elif mutation == "duplicate_experiment":
        child["experiment_id"] = "suite-D0"
    elif mutation == "wrong_variant":
        child["variant"] = "D0"
    elif mutation == "warmup_policy":
        child["warmups"] = 20
    elif mutation == "profile_drift":
        child["profile"]["gc_enabled"] = False
    elif mutation == "profile_hash":
        child["profile_id"] = "0" * 64
    elif mutation == "missing_ram":
        (directory / "D2" / "memory.jsonl").unlink()
    elif mutation == "missing_diagnostics":
        (directory / "D2" / "diagnostic_events.jsonl").unlink()
    elif mutation == "child_failed":
        write_json(directory / "D2" / "state.json", {"status": "FAILED"})
    elif mutation == "missing_events":
        (directory / "suite_events.jsonl").unlink()
    elif mutation in {"duplicate_event", "wrong_sequence", "wrong_event_slot"}:
        events = [json.loads(line) for line in (directory / "suite_events.jsonl").read_text().splitlines()]
        if mutation == "duplicate_event":
            events.append(events[0])
        elif mutation == "wrong_sequence":
            events[0]["sequence"] = 99
        else:
            events[0]["arm"] = "00"
        write_lines(directory / "suite_events.jsonl", events)
    elif mutation == "path_traversal":
        suite["children"]["D2"] = "../outside"
    elif mutation == "symlink_escape":
        external = tmp_path.parent / (tmp_path.name + "-external")
        external.mkdir()
        suite["children"]["D2"] = "D2-link"
        (directory / "D2-link").symlink_to(external, target_is_directory=True)
    elif mutation == "literal_child_symlink":
        external = tmp_path.parent / (tmp_path.name + "-external")
        (directory / "D2").rename(external)
        (directory / "D2").symlink_to(external, target_is_directory=True)
    elif mutation == "malformed_child":
        child["schedule"] = None
    elif mutation == "bad_schema":
        suite["schema_version"] = True
    elif mutation == "numeric_bool_profile":
        child["profile"]["native_tracing"] = 1
    elif mutation == "invalid_event_declaration":
        suite["suite_events"]["version"] = 2
    elif mutation == "bool_event_declaration":
        suite["suite_events"]["version"] = True
    elif mutation == "bool_event_block":
        events = [json.loads(line) for line in (directory / "suite_events.jsonl").read_text().splitlines()]
        events[0]["block"] = True
        write_lines(directory / "suite_events.jsonl", events)
    elif mutation == "bool_outer_block":
        suite["schedule"][0]["outer_block_id"] = True
    write_json(path, child)
    write_json(directory / "suite.json", suite)
    result = suite_module().analyze_suite(directory)
    assert result["valid"] is result["overall_valid"] is False, mutation
    assert result["errors"], mutation
    assert result["comparisons"] is None
    if mutation in {"invalid_event_declaration", "bool_event_declaration", "bool_event_block"}:
        assert result["execution_order"]["status"] == "invalid"
    html = (directory / "suite_report.html").read_text()
    assert "INVALID" in html
    assert 'href="../' not in html
    assert 'href="D2-link/' not in html


def test_suite_events_are_optional_only_without_declaration(diagnostic_suite):
    directory = diagnostic_suite
    suite = json.loads((directory / "suite.json").read_text())
    suite.pop("suite_events")
    write_json(directory / "suite.json", suite)
    (directory / "suite_events.jsonl").unlink()
    result = suite_module().analyze_suite(directory)
    assert result["overall_valid"] is True
    assert result["execution_order"]["status"] == "unavailable"


def test_suite_revalidates_raw_evidence_instead_of_trusting_cached_analysis(diagnostic_suite):
    directory = diagnostic_suite
    first = suite_module().analyze_suite(directory)
    assert first["overall_valid"] is True
    path = directory / "D1" / "requests.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]["output_valid"] = False
    write_lines(path, rows)
    result = suite_module().analyze_suite(directory)
    assert result["overall_valid"] is False
    assert result["children"]["D1"]["overall_valid"] is False
    assert result["comparisons"] is None
    assert (
        first["children"]["D1"]["raw_sha256"]["requests.jsonl"]
        != result["children"]["D1"]["raw_sha256"]["requests.jsonl"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "shift_suite",
        "swap_children",
        "missing_request_time",
        "reversed_request",
        "narrow_slot",
        "missing_memory_time",
        "malformed_memory_time",
    ],
)
def test_declared_suite_order_requires_child_timing_evidence(diagnostic_suite, mutation):
    directory = diagnostic_suite
    events_path = directory / "suite_events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    request_path = directory / "D0" / "requests.jsonl"
    requests = [json.loads(line) for line in request_path.read_text().splitlines()]
    memory_path = directory / "D0" / "memory.jsonl"
    memory = [json.loads(line) for line in memory_path.read_text().splitlines()]
    if mutation == "shift_suite":
        for event in events:
            for key in ("started_at_utc", "finished_at_utc"):
                event[key] = (datetime.fromisoformat(event[key]) + timedelta(days=365)).isoformat()
    elif mutation == "swap_children":
        other = next(event for event in events if event["variant"] == "D1" and event["arm"] == "10")
        requests[0]["started_at"] = other["started_at_utc"]
        requests[0]["finished_at"] = other["finished_at_utc"]
    elif mutation == "missing_request_time":
        requests[0].pop("started_at")
    elif mutation == "reversed_request":
        requests[0]["finished_at"] = events[0]["started_at_utc"]
    elif mutation == "narrow_slot":
        events[0]["finished_at_utc"] = requests[0]["finished_at"]
    elif mutation == "missing_memory_time":
        memory[0].pop("started_at_utc")
    else:
        memory[0]["finished_at_utc"] = "2026-10-02T00:00:00"
    write_lines(events_path, events)
    write_lines(request_path, requests)
    write_lines(memory_path, memory)
    result = suite_module().analyze_suite(directory)
    assert result["overall_valid"] is False
    assert result["execution_order"]["status"] == "invalid"
    assert result["comparisons"] is None
    assert any("suite timing" in error for error in result["errors"])
    assert "INVALID" in (directory / "suite_report.html").read_text()
