"""Offline report gates: incomplete or invalid experiments cannot claim speedup."""
# ruff: noqa: S101, PLR2004, INP001

import copy
import json

import pytest
from benchmark_analyst.campaign_contract import observed_settings, profile_identity
from benchmark_analyst.reporting import _report_html, analyze


@pytest.fixture
def campaign(tmp_path):
    """Small, complete four-block experiment with hand-calculable effects."""
    orders = [
        ["00", "01", "11", "10"],
        ["01", "10", "00", "11"],
        ["10", "11", "01", "00"],
        ["11", "00", "10", "01"],
    ]
    manifest = {
        "schema_version": 1,
        "experiment_id": "fixture",
        "requests_per_arm": 8,
        "blocks": 4,
        "warmups": 1,
        "primary_metric": "langflow_overhead_ms",
        "schedule": [
            {"block": block, "arm": arm, "count": 2} for block, order in enumerate(orders, start=1) for arm in order
        ],
        "workload": {
            "scrfd_node_ids": ["load", "detect", "draw", "save"],
            "reference_pixel_sha256": "a" * 64,
            "flow_sha256": "b" * 64,
            "input_sha256": "c" * 64,
            "model_sha256": "d" * 64,
        },
        "source": {"git_revision": "e" * 40, "sha256": "f" * 64, "files": {"observer.py": "f" * 64}},
    }
    rows, evidence = [], []
    base = {"00": 100, "01": 70, "10": 80, "11": 40}
    for slot, entry in enumerate(manifest["schedule"]):
        block, arm = entry["block"], entry["arm"]
        pid = 1000 + slot
        for phase, count in [("warmup", 1), ("measured", 2)]:
            for index in range(count):
                overhead = base[arm] + (2 * index if phase == "measured" else 0)
                rows.append(
                    {
                        "block": block,
                        "arm": arm,
                        "phase": phase,
                        "index": index,
                        "request_id": f"{block}-{arm}-{phase}-{index}",
                        "pid": pid,
                        "outcome": "success",
                        "error": None,
                        "output_valid": True,
                        "langflow_overhead_ms": overhead,
                        "server_total_ms": overhead + 40,
                        "scrfd_processing_ms": 40,
                        "flow_api_ms": overhead + 43,
                        "upload_ms": 5000,
                        "warm_path": "warm" if arm[1] == "1" and phase == "measured" else "cold",
                        "image_pixel_sha256": "a" * 64,
                        "component_intervals_ms": [
                            {"node_id": name, "start_ms": i * 10, "end_ms": (i + 1) * 10}
                            for i, name in enumerate(["load", "detect", "draw", "save"])
                        ],
                    }
                )
        snapshots = {}
        for name, count in [("before_warmup", 0), ("after_warmup", 1), ("after_measurement", 3)]:
            snapshots[name] = {
                "pid": pid,
                "settings": {
                    "component_compilation_cache_enabled": arm[0] == "1",
                    "warm_registry_enabled": arm[1] == "1",
                    "http_connection_reuse_enabled": False,
                },
                "compilation": {"hits": count if arm[0] == "1" else 0, "bypasses": count if arm[0] == "0" else 0},
                "warm": {
                    "hits": max(count - 1, 0) if arm[1] == "1" else 0,
                    "cold": min(count, 1) if arm[1] == "1" else count,
                },
                "registry_entries": int(arm[1] == "1" and count > 0),
            }
        evidence.append({"block": block, "arm": arm, **snapshots})
    return tmp_path, manifest, rows, evidence


def write_campaign(campaign):
    directory, manifest, rows, evidence = campaign
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for filename, records in [("requests.jsonl", rows), ("worker_evidence.jsonl", evidence)]:
        (directory / filename).write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    return directory


def test_reports_primary_overhead_percentiles_and_factorial_contrasts(campaign):
    result = analyze(write_campaign(campaign))
    assert result["valid"] is True
    assert result["is_full_campaign"] is False
    summary = result["arms"]["00"]["langflow_overhead_ms"]
    assert summary == {"n": 8, "mean": 101.0, "p50": 101.0, "p95": 102.0, "p99": 102.0}
    assert result["blocks"]["1"]["11"]["langflow_overhead_ms"]["mean"] == 41.0
    contrasts = result["contrasts"]
    assert contrasts["vs_00"]["10"]["delta_ms"] == -20.0
    assert contrasts["vs_00"]["01"]["delta_ms"] == -30.0
    assert contrasts["vs_00"]["11"]["delta_ms"] == -60.0
    assert contrasts["conditional"]["compilation_when_warm_on"]["delta_ms"] == -30.0
    assert contrasts["conditional"]["warm_when_compilation_on"]["delta_ms"] == -40.0
    assert contrasts["interaction_ms"] == -10.0
    assert contrasts["vs_00"]["11"]["reduction_pct"] == pytest.approx(60 / 101 * 100)
    for name in ["analysis.json", "report.md", "report.html", "summary.csv", "charts.png"]:
        assert (campaign[0] / name).stat().st_size > 0
    assert (campaign[0] / "charts.png").read_bytes().startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_measured",
        "duplicate_measured",
        "missing_warmup",
        "error",
        "timeout",
        "bad_output",
        "nan",
        "negative",
        "wrong_subtraction",
        "missing_span",
        "wrong_union",
        "wrong_pid",
        "wrong_path",
        "wrong_pixels",
        "missing_evidence",
        "wrong_flags",
        "http_reuse",
        "no_warm_hits",
        "warm_fallback",
        "off_warm_hits",
        "no_compilation_hits",
        "off_compilation_hits",
        "off_no_bypasses",
        "counter_reset",
        "duplicate_evidence",
        "unbalanced_schedule",
        "unknown_slot",
        "missing_source",
        "missing_workload",
    ],
)
def test_invalid_campaign_retains_diagnostics_but_cannot_claim_speedup(campaign, mutation):
    _, manifest, rows, evidence = campaign
    row = next(row for row in rows if row["phase"] == "measured")
    warm = next(item for item in evidence if item["arm"] == "01")
    compiled = next(item for item in evidence if item["arm"] == "10")
    if mutation == "missing_measured":
        rows.remove(row)
    elif mutation == "duplicate_measured":
        rows.append(dict(row))
    elif mutation == "missing_warmup":
        rows.pop(0)
    elif mutation in {"error", "timeout"}:
        row.update(outcome=mutation, error="failed")
    elif mutation == "bad_output":
        row["output_valid"] = False
    elif mutation == "nan":
        row["langflow_overhead_ms"] = float("nan")
    elif mutation == "negative":
        row["upload_ms"] = -1
    elif mutation == "wrong_subtraction":
        row["langflow_overhead_ms"] += 10
    elif mutation == "missing_span":
        row["component_intervals_ms"].pop()
    elif mutation == "wrong_union":
        row["component_intervals_ms"][1].update(start_ms=0, end_ms=10)
    elif mutation == "wrong_pid":
        row["pid"] = 9999
    elif mutation == "wrong_path":
        row["warm_path"] = "warm"
    elif mutation == "wrong_pixels":
        row["image_pixel_sha256"] = "0" * 64
    elif mutation == "missing_evidence":
        evidence.pop()
    elif mutation == "wrong_flags":
        evidence[0]["after_measurement"]["settings"]["warm_registry_enabled"] = True
    elif mutation == "http_reuse":
        evidence[0]["after_warmup"]["settings"]["http_connection_reuse_enabled"] = True
    elif mutation == "no_warm_hits":
        warm["after_measurement"]["warm"]["hits"] = 0
    elif mutation == "warm_fallback":
        warm["after_measurement"]["warm"]["cold"] = 2
    elif mutation == "off_warm_hits":
        evidence[0]["after_measurement"]["warm"]["hits"] = 1
    elif mutation == "no_compilation_hits":
        compiled["after_measurement"]["compilation"]["hits"] = 0
    elif mutation == "off_compilation_hits":
        evidence[0]["after_measurement"]["compilation"]["hits"] = 1
    elif mutation == "off_no_bypasses":
        for phase in ["before_warmup", "after_warmup", "after_measurement"]:
            evidence[0][phase]["compilation"]["bypasses"] = 0
    elif mutation == "counter_reset":
        compiled["before_warmup"]["compilation"]["hits"] = 20
    elif mutation == "duplicate_evidence":
        evidence.append(evidence[0])
    elif mutation == "unbalanced_schedule":
        manifest["schedule"][0], manifest["schedule"][1] = manifest["schedule"][1], manifest["schedule"][0]
    elif mutation == "unknown_slot":
        row["arm"] = "99"
    elif mutation == "missing_source":
        manifest["source"] = {}
    elif mutation == "missing_workload":
        manifest["workload"] = {}
    result = analyze(write_campaign(campaign))
    assert result["valid"] is False, mutation
    assert result["validation_errors"], mutation
    assert result["contrasts"] is None
    assert "INVALID" in (campaign[0] / "report.md").read_text(encoding="utf-8")
    assert "Không kết luận tăng tốc" in (campaign[0] / "report.html").read_text(encoding="utf-8")


def test_overlapping_component_intervals_are_excluded_once(campaign):
    row = campaign[2][0]
    row["component_intervals_ms"] = [
        {"node_id": node, "start_ms": start, "end_ms": end}
        for node, start, end in [("load", 0, 20), ("detect", 10, 30), ("draw", 30, 35), ("save", 35, 40)]
    ]
    result = analyze(write_campaign(campaign))
    assert result["valid"] is True


def test_chat_component_spans_are_kept_in_langflow_overhead(campaign):
    row = campaign[2][0]
    row["component_intervals_ms"].extend(
        [
            {"node_id": "chat-input", "start_ms": 40, "end_ms": 60},
            {"node_id": "chat-output", "start_ms": 60, "end_ms": 80},
        ]
    )
    result = analyze(write_campaign(campaign))
    assert result["valid"] is True
    assert result["arms"]["00"]["langflow_overhead_ms"]["mean"] == 101.0


def test_malformed_jsonl_and_missing_manifest_produce_invalid_report(tmp_path):
    (tmp_path / "requests.jsonl").write_text('{"truncated":', encoding="utf-8")
    result = analyze(tmp_path)
    assert result["valid"] is False
    assert any("manifest.json" in message for message in result["validation_errors"])
    assert any("requests.jsonl:1" in message for message in result["validation_errors"])
    assert json.loads((tmp_path / "analysis.json").read_text())["contrasts"] is None


def test_html_escapes_untrusted_manifest_text(campaign):
    campaign[1]["experiment_id"] = '<script>alert("oops")</script>'
    analyze(write_campaign(campaign))
    rendered = (campaign[0] / "report.html").read_text(encoding="utf-8")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


@pytest.mark.parametrize("status", ["RUNNING", "INCOMPLETE", "unknown", None])
def test_incomplete_runner_state_prevents_valid_claim_even_with_all_rows(campaign, status):
    directory = write_campaign(campaign)
    (directory / "state.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    result = analyze(directory)
    assert result["valid"] is False
    assert result["contrasts"] is None
    assert any("state.json" in message for message in result["validation_errors"])


def test_completed_runner_state_and_raw_files_remain_unchanged(campaign):
    directory = write_campaign(campaign)
    (directory / "state.json").write_text('{"status":"COMPLETE"}', encoding="utf-8")
    names = ["manifest.json", "requests.jsonl", "worker_evidence.jsonl", "state.json"]
    original = {name: (directory / name).read_bytes() for name in names}
    result = analyze(directory)
    assert result["valid"] is True
    assert {name: (directory / name).read_bytes() for name in names} == original


def diagnostic_campaign(campaign):
    directory, manifest, rows, evidence = campaign
    orders = [("10", "11"), ("11", "10"), ("11", "10"), ("10", "11")]
    manifest["kind"] = "diagnostic"
    manifest["schedule"] = [
        {"block": block, "arm": arm, "count": 2} for block, order in enumerate(orders, 1) for arm in order
    ]
    selected = [(item["block"], item["arm"]) for item in manifest["schedule"]]
    rows = [row for slot in selected for row in rows if (row["block"], row["arm"]) == slot]
    evidence = [item for slot in selected for item in evidence if (item["block"], item["arm"]) == slot]
    bind_test_profile(manifest, evidence)
    return directory, manifest, rows, evidence


def test_diagnostic_child_uses_balanced_pairs_without_factorial_claim(campaign):
    result = analyze(write_campaign(diagnostic_campaign(campaign)))
    assert result["valid"] is True
    assert result["overall_valid"] is True
    assert result["kind"] == "diagnostic"
    assert result["is_full_campaign"] is False
    assert set(result["arms"]) == {"10", "11"}
    assert result["contrasts"] is None
    assert result["tail_diagnostics"]["paired_11_minus_10"]["1"]["mean"] == -40
    assert "DIAGNOSTIC" in (campaign[0] / "report.html").read_text()


@pytest.mark.parametrize("kind", ["unsupported", None, 123])
def test_unknown_manifest_kind_is_invalid(campaign, kind):
    campaign[1]["kind"] = kind
    result = analyze(write_campaign(campaign))
    assert result["valid"] is False
    assert any("kind" in message for message in result["validation_errors"])


def test_diagnostic_wrong_pair_order_is_invalid(campaign):
    data = diagnostic_campaign(campaign)
    data[1]["schedule"][0], data[1]["schedule"][1] = data[1]["schedule"][1], data[1]["schedule"][0]
    result = analyze(write_campaign(data))
    assert result["valid"] is False
    assert any("balanced" in message for message in result["validation_errors"])


def sequential_method_spans(rows):
    for row in rows:
        total = row["server_total_ms"]
        row["component_intervals_ms"] = [
            {"node_id": node, "start_ms": start, "end_ms": end}
            for node, start, end in [
                ("ChatInput-fixture", 10, 12),
                ("load", 15, 25),
                ("detect", 27, 37),
                ("draw", 39, 49),
                ("save", 51, 61),
                ("ChatOutput-fixture", total - 7, total - 5),
            ]
        ]


def test_six_method_decomposition_reconstructs_overhead_and_retains_spikes(campaign):
    row = next(row for row in campaign[2] if row["arm"] == "10" and row["phase"] == "measured")
    for key in ["langflow_overhead_ms", "server_total_ms", "flow_api_ms"]:
        row[key] += 300
    sequential_method_spans(campaign[2])
    result = analyze(write_campaign(campaign))
    assert result["valid"] is True
    decomposition = result["setup_decomposition"]
    assert decomposition["status"] == "measured"
    assert len(decomposition["records"]) == 32
    for record in decomposition["records"]:
        assert sum(record[key] for key in ["pre_ms", "gaps_ms", "post_ms", "chat_ms"]) == pytest.approx(
            record["overhead_ms"]
        )
    assert decomposition["arms"]["10"]["pre_ms"]["mean"] == 10
    assert result["tail_diagnostics"]["spikes"][0]["request_id"] == row["request_id"]
    assert result["tail_diagnostics"]["spike_threshold_ms"] == 200
    assert result["tail_diagnostics"]["window_size"] == 50
    assert result["arms"]["10"]["langflow_overhead_ms"]["n"] == 8
    assert (campaign[0] / "diagnostics.png").read_bytes().startswith(b"\x89PNG")


@pytest.mark.parametrize("mutation", ["missing_chat", "overlap_chat"])
def test_unavailable_decomposition_preserves_valid_scrfd_union(campaign, mutation):
    sequential_method_spans(campaign[2])
    row = next(row for row in campaign[2] if row["phase"] == "measured")
    if mutation == "missing_chat":
        row["component_intervals_ms"].pop()
    else:
        row["component_intervals_ms"][0].update(start_ms=14, end_ms=16)
    result = analyze(write_campaign(campaign))
    assert result["valid"] is True
    assert result["setup_decomposition"]["status"] == "partial"
    assert result["setup_decomposition"]["unavailable"][0]["request_id"] == row["request_id"]


def test_legacy_ram_is_unavailable_and_report_states_workload_scope(campaign):
    result = analyze(write_campaign(campaign))
    assert result["overall_valid"] == result["valid"] is True
    assert result["resource_validity"]["status"] == "unavailable"
    assert result["resource_validity"]["requested"] is False
    assert result["diagnostic_validity"]["requested"] is False
    report = (campaign[0] / "report.html").read_text()
    assert "RAM chưa đo" in report
    assert "một flow" in report
    assert "peak RAM" in report
    assert "heap" in report


def test_html_renderer_allows_only_literal_report_image_filenames():
    rendered = _report_html(
        "\n".join(
            [
                "![a](charts.png)",
                "![b](tradeoffs.png)",
                "![c](diagnostics.png)",
                "![bad](https://example.com/x.png)",
                "![bad](../charts.png)",
                '![bad](charts.png" onerror="alert(1))',
            ]
        )
    )
    assert rendered.count("<img ") == 3
    for name in ["charts.png", "tradeoffs.png", "diagnostics.png"]:
        assert f'src="{name}"' in rendered
    assert "onerror" not in rendered


def test_declared_missing_memory_invalidates_resources_only(campaign):
    campaign[1]["resource_measurement"] = {
        "version": 1,
        "enabled": True,
        "required": ["rss"],
        "optional": ["uss"],
        "checkpoints": ["before_warmup", "after_warmup", "after_measurement", "after_idle"],
        "idle_seconds": 5,
    }
    bind_test_profile(campaign[1], campaign[3])
    result = analyze(write_campaign(campaign))
    assert result["valid"] is True
    assert result["resource_validity"]["valid"] is False
    assert result["overall_valid"] is False
    assert result["contrasts"] is not None
    report = (campaign[0] / "report.html").read_text()
    assert "overall: INVALID" in report
    assert "Latency/cache/output: VALID" in report


def bind_test_profile(manifest, evidence):
    effective = {
        "native_tracing": True,
        "gc_enabled": True,
        "product_telemetry_enabled": True,
        "uvicorn_access_enabled": False,
    }
    manifest["profile_id"], manifest["profile"] = profile_identity(
        manifest.get("fixture_sha256"),
        diagnostics=manifest.get("diagnostics", {}).get("enabled", False),
        telemetry=True,
        warmups=manifest["warmups"],
        effective_common_settings=observed_settings({"effective": effective}),
    )
    for record in evidence:
        record.setdefault("after_idle", copy.deepcopy(record["after_measurement"]))
        for phase in ("before_warmup", "after_warmup", "after_measurement", "after_idle"):
            record[phase]["effective"] = copy.deepcopy(effective)


def write_diagnostics(campaign):
    directory, manifest, rows, evidence = campaign
    manifest["diagnostics"] = {
        "version": 1,
        "enabled": True,
        "detail": "coarse",
        "max_events": 8192,
        "lag_interval_ms": 20,
        "required_capabilities": ["setup", "components", "gc", "event_loop_lag"],
    }
    bind_test_profile(manifest, evidence)
    events, metadata = [], []
    for item in evidence:
        slot_events = []
        for row in rows:
            if (row["block"], row["arm"]) != (item["block"], item["arm"]):
                continue
            prefix = row["request_id"]
            span_data = [("request", "asgi_request", 0, row["server_total_ms"], None)]
            first_method = min(span["start_ms"] for span in row["component_intervals_ms"])
            span_data.append(("setup", "pre_component", 0, first_method, None))
            span_data.extend(
                ("component", "component_body", span["start_ms"], span["end_ms"], span["node_id"])
                for span in row["component_intervals_ms"]
            )
            for index, (category, name, start, end, node) in enumerate(span_data):
                event = {
                    "schema_version": 1,
                    "experiment_id": manifest["experiment_id"],
                    "block": row["block"],
                    "arm": row["arm"],
                    "pid": row["pid"],
                    "process_create_time": float(row["pid"]),
                    "event_id": f"{prefix}-{index}",
                    "request_id": prefix,
                    "parent_id": f"{prefix}-0" if index else None,
                    "category": category,
                    "name": name,
                    "start_ns": round(start * 1000000),
                    "end_ns": round(end * 1000000),
                    "thread_id": 1,
                    "outcome": "success",
                }
                if node:
                    event["node_id"] = node
                slot_events.append(event)
        for checkpoint in ["before_warmup", "after_warmup", "after_measurement", "after_idle"]:
            item[checkpoint]["process_create_time"] = float(item[checkpoint]["pid"])
        metadata.append(
            {
                "experiment_id": manifest["experiment_id"],
                "block": item["block"],
                "arm": item["arm"],
                "metadata": {
                    "enabled": True,
                    "detail": "coarse",
                    "clock": "worker_monotonic",
                    "unit": "ns",
                    "pid": item["after_measurement"]["pid"],
                    "process_create_time": float(item["after_measurement"]["pid"]),
                    "drained": len(slot_events),
                    "total_drained": len(slot_events),
                    "dropped": 0,
                    "truncated": 0,
                    "unfinished": 0,
                    "capabilities": {key: True for key in manifest["diagnostics"]["required_capabilities"]},
                },
            }
        )
        events.extend(slot_events)
    write_campaign(campaign)
    for name, records in [("diagnostic_events.jsonl", events), ("diagnostic_metadata.jsonl", metadata)]:
        (directory / name).write_text("".join(json.dumps(record) + "\n" for record in records))
    return events, metadata


def test_diagnostic_sidecars_validate_identity_coverage_union_and_raw_hashes(campaign):
    events, _ = write_diagnostics(campaign)
    names = [
        "manifest.json",
        "requests.jsonl",
        "worker_evidence.jsonl",
        "diagnostic_events.jsonl",
        "diagnostic_metadata.jsonl",
    ]
    original = {name: (campaign[0] / name).read_bytes() for name in names}
    result = analyze(campaign[0])
    diagnostic = result["diagnostic_validity"]
    assert result["valid"] is True
    assert result["overall_valid"] is True
    assert diagnostic["valid"] is True
    assert diagnostic["status"] == "measured"
    assert diagnostic["event_count"] == len(events)
    assert diagnostic["coverage"]["requests"] == len(campaign[2])
    assert diagnostic["phase_summary"]["component/component_body"]["mean"] == 40
    assert {name: (campaign[0] / name).read_bytes() for name in names} == original
    assert set(result["raw_sha256"]) >= set(names)


@pytest.mark.parametrize(
    "mutation",
    [
        "dropped",
        "truncated",
        "unfinished",
        "clock",
        "drained",
        "missing_setup",
        "wrong_pid",
        "outside_parent",
        "missing_metadata",
    ],
)
def test_invalid_diagnostic_evidence_cannot_invalidate_legacy_latency(campaign, mutation):
    events, metadata = write_diagnostics(campaign)
    if mutation in {"dropped", "truncated", "unfinished"}:
        metadata[0]["metadata"][mutation] = 1
    elif mutation == "clock":
        metadata[0]["metadata"]["clock"] = "client"
    elif mutation == "drained":
        metadata[0]["metadata"]["drained"] += 1
    elif mutation == "missing_setup":
        events[1]["name"] = "different"
    elif mutation == "wrong_pid":
        events[0]["pid"] += 1
    elif mutation == "outside_parent":
        events[1]["end_ns"] = events[0]["end_ns"] + 1
    else:
        metadata.pop()
    for name, records in [("diagnostic_events.jsonl", events), ("diagnostic_metadata.jsonl", metadata)]:
        (campaign[0] / name).write_text("".join(json.dumps(record) + "\n" for record in records))
    result = analyze(campaign[0])
    assert result["valid"] is True
    assert result["diagnostic_validity"]["valid"] is False
    assert result["diagnostic_validity"]["errors"]
    assert result["overall_valid"] is False


def write_resource_campaign(campaign):
    directory, manifest, _, evidence = campaign
    checkpoints = ["before_warmup", "after_warmup", "after_measurement", "after_idle"]
    manifest["resource_measurement"] = {
        "version": 1,
        "enabled": True,
        "required": ["rss"],
        "optional": ["uss"],
        "checkpoints": checkpoints,
        "idle_seconds": 5,
    }
    bind_test_profile(manifest, evidence)
    memory = []
    levels = {"00": 120, "01": 125, "10": 115, "11": 112}
    for item in evidence:
        pid = item["after_measurement"]["pid"]
        for checkpoint in checkpoints:
            item[checkpoint]["process_create_time"] = float(pid)
            item[checkpoint]["cache_accounting"] = {
                "compilation": {"entries": 6, "max_entries": 512, "source_utf8_bytes": 1000},
                "warm": {"entries": 1, "resident_json_bytes": 2000, "max_entries": 256},
            }
        for index, checkpoint in enumerate(checkpoints):
            timestamp = f"2026-10-02T00:00:{index * 10:02d}+00:00"
            value = (100 if index == 0 else levels[item["arm"]]) * 1048576
            memory.append(
                {
                    "schema_version": 1,
                    "experiment_id": manifest["experiment_id"],
                    "block": item["block"],
                    "arm": item["arm"],
                    "checkpoint": checkpoint,
                    "pid": pid,
                    "process_create_time": float(pid),
                    "elapsed_since_last_sample_ms": None if index < 2 else 0 if index == 2 else 5000,
                    "started_at_utc": timestamp,
                    "finished_at_utc": f"2026-10-02T00:00:{index * 10 + 2:02d}+00:00",
                    "collector": {"name": "psutil", "version": "7.2.2"},
                    "rss": {
                        "bytes": value,
                        "status": "measured",
                        "reason": None,
                        "started_at_utc": timestamp,
                        "duration_ms": 0.1,
                    },
                    "uss": {
                        "bytes": None,
                        "status": "unavailable",
                        "reason": "AccessDenied",
                        "started_at_utc": f"2026-10-02T00:00:{index * 10 + 1:02d}+00:00",
                        "duration_ms": 0.1,
                    },
                    "cache_accounting": {
                        "started_at_utc": timestamp,
                        "duration_ms": 0,
                        "snapshot": item["after_measurement"]["cache_accounting"],
                    },
                }
            )
    write_campaign(campaign)
    (directory / "memory.jsonl").write_text("".join(json.dumps(row) + "\n" for row in memory))
    return memory


def test_complete_resource_report_keeps_signed_deltas_and_optional_uss_denial(campaign):
    write_resource_campaign(campaign)
    directory = campaign[0]
    original = (directory / "memory.jsonl").read_bytes()
    result = analyze(directory)
    assert result["overall_valid"] is True
    assert result["resource_validity"]["contrasts"]["marginal_11_10"]["after_idle"]["rss"]["mean"] == -3 * 1048576
    assert (
        result["worker_diagnostics"][0]["cache_occupancy"]["after_measurement"]["cache_accounting"]["compilation"][
            "max_entries"
        ]
        == 512
    )
    assert (directory / "memory.jsonl").read_bytes() == original
    for filename in ("tradeoffs.png", "resource_summary.csv"):
        assert (directory / filename).stat().st_size > 0
    report = (directory / "report.html").read_text()
    assert "AccessDenied" in report
    assert "-3.000" in report
    assert 'src="tradeoffs.png"' in report
    assert result["tradeoffs"]["01"]["overhead_saved_ms"] == 30
    assert result["tradeoffs"]["01"]["ms_saved_per_added_mib"] == 6
    assert result["tradeoffs"]["10"]["ms_saved_per_added_mib"] is None


def test_cache_counters_use_event_hits_and_misses_without_request_assumption(campaign):
    compiled = next(item for item in campaign[3] if item["arm"] == "10")
    for phase, misses, builds, evictions in [
        ("before_warmup", 0, 0, 0),
        ("after_warmup", 6, 6, 0),
        ("after_measurement", 7, 7, 1),
    ]:
        compiled[phase]["compilation"].update(misses=misses, builds=builds, evictions=evictions)
    result = analyze(write_campaign(campaign))
    item = next(item for item in result["worker_diagnostics"] if item["block"] == 1 and item["arm"] == "10")
    assert item["compilation_misses"] == 1
    assert item["compilation_builds"] == 1
    assert item["compilation_evictions"] == 1
    assert item["compilation_hit_rate"] == pytest.approx(2 / 3)
    legacy = next(item for item in result["worker_diagnostics"] if item["block"] == 2 and item["arm"] == "10")
    assert legacy["compilation_hit_rate"] is None


@pytest.mark.parametrize("mutation", ["event_id", "request_id", "parent_id", "start_ns", "category"])
def test_malformed_diagnostic_fields_render_invalid_without_crashing(campaign, mutation):
    events, _ = write_diagnostics(campaign)
    events[0][mutation] = []
    (campaign[0] / "diagnostic_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(campaign[0])
    assert result["valid"] is True
    assert result["diagnostic_validity"]["valid"] is False


def test_nested_phase_union_summaries_keep_arm_identity_and_render_phase_evidence(campaign):
    events, metadata = write_diagnostics(campaign)
    row = next(row for row in campaign[2] if row["phase"] == "measured" and row["arm"] == "10")
    parent = next(
        event for event in events if event["request_id"] == row["request_id"] and event["category"] == "request"
    )
    for index, (start, end) in enumerate([(5, 15), (10, 20)]):
        events.append(
            {
                **parent,
                "event_id": f"nested-{index}",
                "parent_id": parent["event_id"],
                "category": "setup",
                "name": "class_preparation",
                "start_ns": start * 1000000,
                "end_ns": end * 1000000,
            }
        )
    item = next(item for item in metadata if (item["block"], item["arm"]) == (row["block"], row["arm"]))
    item["metadata"]["drained"] += 2
    item["metadata"]["total_drained"] += 2
    for name, records in [("diagnostic_events.jsonl", events), ("diagnostic_metadata.jsonl", metadata)]:
        (campaign[0] / name).write_text("".join(json.dumps(record) + "\n" for record in records))
    result = analyze(campaign[0])
    assert result["diagnostic_validity"]["valid"] is True
    assert result["diagnostic_validity"]["phase_summary"]["setup/class_preparation"]["mean"] == 15
    assert result["diagnostic_validity"]["phase_summary_by_arm"]["10"]["setup/class_preparation"]["mean"] == 15
    assert "setup/class_preparation" in (campaign[0] / "report.html").read_text()


@pytest.mark.parametrize("mutation", ["span_start", "span_node", "workload_nodes", "request_index", "schedule"])
def test_malformed_raw_evidence_still_writes_invalid_report(campaign, mutation):
    write_diagnostics(campaign)
    row = next(row for row in campaign[2] if row["phase"] == "measured")
    if mutation == "span_start":
        row["component_intervals_ms"][0]["start_ms"] = None
    elif mutation == "span_node":
        row["component_intervals_ms"][0]["node_id"] = []
    elif mutation == "workload_nodes":
        campaign[1]["workload"]["scrfd_node_ids"] = [[]]
    elif mutation == "request_index":
        row["index"] = "invalid"
    else:
        campaign[1]["schedule"] = None
    write_campaign(campaign)
    result = analyze(campaign[0])
    assert result["valid"] is False
    assert result["overall_valid"] is False
    assert (campaign[0] / "report.html").is_file()


def test_declared_diagnostics_require_worker_create_time_at_every_snapshot(campaign):
    write_diagnostics(campaign)
    campaign[3][0]["before_warmup"].pop("process_create_time")
    write_campaign(campaign)
    result = analyze(campaign[0])
    assert result["valid"] is True
    assert result["diagnostic_validity"]["valid"] is False
    assert result["overall_valid"] is False


def test_report_links_diagnostic_sources_and_retains_effective_profile(campaign):
    write_diagnostics(campaign)
    campaign[1]["profile_id"] = "profile-hash"
    campaign[1]["fixture"] = {"content_sha256": "1" * 64}
    campaign[3][0]["after_measurement"]["effective"] = {"gc_enabled": True, "uvicorn_access_enabled": False}
    write_campaign(campaign)
    result = analyze(campaign[0])
    assert result["worker_diagnostics"][0]["effective"] == {"gc_enabled": True, "uvicorn_access_enabled": False}
    report = (campaign[0] / "report.html").read_text()
    assert '<a href="diagnostic_events.jsonl">' in report
    assert '<a href="diagnostic_metadata.jsonl">' in report
    assert "profile-hash" in report
    assert "uvicorn_access_enabled" in report


@pytest.mark.parametrize("declaration", [[], "invalid", {"enabled": "true"}, {"version": 1}])
def test_malformed_diagnostic_declaration_is_invalid_evidence(campaign, declaration):
    campaign[1]["diagnostics"] = declaration
    result = analyze(write_campaign(campaign))
    assert result["valid"] is True
    assert result["diagnostic_validity"]["valid"] is False
    assert result["overall_valid"] is False


def test_analyze_removes_stale_optional_derived_artifacts_without_raw_changes(campaign):
    directory = write_campaign(campaign)
    original = (directory / "requests.jsonl").read_bytes()
    for name in ("tradeoffs.png", "diagnostics.png", "resource_summary.csv"):
        (directory / name).write_bytes(b"old derived content")
    result = analyze(directory)
    assert result["valid"] is True
    for name in ("tradeoffs.png", "diagnostics.png", "resource_summary.csv"):
        assert not (directory / name).exists()
    assert (directory / "requests.jsonl").read_bytes() == original


def contextual_nuisance_fixture(campaign, category="gc", name="gc_collection"):
    events, metadata = write_diagnostics(campaign)
    row = next(row for row in campaign[2] if row["phase"] == "measured" and row["arm"] == "10")
    root = next(
        event for event in events if event["request_id"] == row["request_id"] and event["category"] == "request"
    )
    graph = {
        **root,
        "event_id": "graph-parent",
        "parent_id": root["event_id"],
        "category": "execution",
        "name": "graph_execution",
        "end_ns": 45000000,
    }
    nuisance = {
        **root,
        "event_id": "contextual-nuisance",
        "parent_id": graph["event_id"],
        "category": category,
        "name": name,
        "start_ns": 50000000,
        "end_ns": 51000000,
    }
    background = {
        **nuisance,
        "event_id": "background-nuisance",
        "request_id": None,
        "parent_id": None,
        "start_ns": root["end_ns"] - 500000,
        "end_ns": root["end_ns"] + 1000000,
    }
    events.extend([graph, nuisance, background])
    item = next(item for item in metadata if (item["block"], item["arm"]) == (row["block"], row["arm"]))
    item["metadata"]["drained"] += 3
    item["metadata"]["total_drained"] += 3
    return events, metadata, root, graph, nuisance, row


def save_diagnostic_fixture(directory, events, metadata):
    for name, records in [("diagnostic_events.jsonl", events), ("diagnostic_metadata.jsonl", metadata)]:
        (directory / name).write_text("".join(json.dumps(record) + "\n" for record in records))


@pytest.mark.parametrize(("category", "name"), [("gc", "gc_collection"), ("event_loop", "event_loop_lag")])
def test_nuisance_context_parent_can_end_before_event_without_becoming_a_phase(campaign, category, name):
    events, metadata, _, _, _, row = contextual_nuisance_fixture(campaign, category, name)
    directory = campaign[0]
    save_diagnostic_fixture(directory, events, metadata)
    raw_names = (
        "manifest.json",
        "requests.jsonl",
        "worker_evidence.jsonl",
        "diagnostic_events.jsonl",
        "diagnostic_metadata.jsonl",
    )
    original = {filename: (directory / filename).read_bytes() for filename in raw_names}
    result = analyze(directory)
    diagnostic = result["diagnostic_validity"]
    assert diagnostic["valid"] is True
    assert result["overall_valid"] is True
    assert f"{category}/{name}" not in diagnostic["phase_summary"]
    assert all(f"{category}/{name}" not in phases for phases in diagnostic["phase_summary_by_arm"].values())
    correlation = next(item for item in diagnostic["correlations"] if item["request_id"] == row["request_id"])
    assert {"contextual-nuisance", "background-nuisance"}.issubset(correlation["overlap_event_ids"])
    assert correlation["scope"] == "whole_worker_process"
    assert correlation["overlap_rule"] == "positive_timestamp_intersection"
    assert diagnostic["source_refs"]["analyzer"]["semantics_version"] == 2
    assert len(diagnostic["source_refs"]["analyzer"]["source_sha256"]) == 64
    assert diagnostic["source_refs"]["collector_source_sha256"] == campaign[1]["source"].get("sha256")
    assert {filename: (directory / filename).read_bytes() for filename in raw_names} == original


@pytest.mark.parametrize(
    ("category", "name"), [("setup", "late_setup"), ("gc", "other_gc"), ("event_loop", "other_lag")]
)
def test_only_exact_nuisance_pairs_can_have_contextual_parent_links(campaign, category, name):
    events, metadata, *_ = contextual_nuisance_fixture(campaign, category, name)
    save_diagnostic_fixture(campaign[0], events, metadata)
    diagnostic = analyze(campaign[0])["diagnostic_validity"]
    assert diagnostic["valid"] is False
    assert "diagnostic events: child interval outside parent" in diagnostic["errors"]


@pytest.mark.parametrize(
    "mutation", ["unknown_parent", "cross_pid", "cross_create_time", "cross_request", "cycle", "outside_asgi"]
)
def test_contextual_nuisance_parent_does_not_bypass_identity_or_envelope_checks(campaign, mutation):
    events, metadata, root, graph, nuisance, row = contextual_nuisance_fixture(campaign)
    if mutation == "unknown_parent":
        nuisance["parent_id"] = "absent-parent"
    elif mutation == "cross_pid":
        graph["pid"] += 1000
    elif mutation == "cross_create_time":
        graph["process_create_time"] += 1
    elif mutation == "cross_request":
        other = next(
            event
            for event in events
            if event["request_id"] != row["request_id"]
            and event["category"] == "request"
            and (event["block"], event["arm"]) == (row["block"], row["arm"])
        )
        nuisance["parent_id"] = other["event_id"]
    elif mutation == "cycle":
        nuisance["parent_id"] = "background-nuisance"
        background = next(event for event in events if event["event_id"] == "background-nuisance")
        background.update(
            request_id=row["request_id"],
            parent_id=nuisance["event_id"],
            start_ns=nuisance["start_ns"],
            end_ns=nuisance["end_ns"],
        )
    else:
        nuisance.update(start_ns=root["end_ns"] + 1, end_ns=root["end_ns"] + 2)
    save_diagnostic_fixture(campaign[0], events, metadata)
    result = analyze(campaign[0])
    assert result["valid"] is True
    assert result["diagnostic_validity"]["valid"] is False
    assert result["overall_valid"] is False


@pytest.mark.parametrize(("category", "name"), [("gc", "gc_collection"), ("event_loop", "event_loop_lag")])
@pytest.mark.parametrize("touch", ["endpoint", "zero_duration_inside"])
def test_nuisance_overlap_requires_positive_intersection(campaign, category, name, touch):
    events, metadata, root, _, nuisance, row = contextual_nuisance_fixture(campaign, category, name)
    instant = root["end_ns"] if touch == "endpoint" else nuisance["start_ns"]
    touching = {
        **nuisance,
        "event_id": "touching-nuisance",
        "request_id": None,
        "parent_id": None,
        "start_ns": instant,
        "end_ns": instant + 1000000 if touch == "endpoint" else instant,
    }
    events.append(touching)
    item = next(item for item in metadata if (item["block"], item["arm"]) == (row["block"], row["arm"]))
    item["metadata"]["drained"] += 1
    item["metadata"]["total_drained"] += 1
    save_diagnostic_fixture(campaign[0], events, metadata)
    diagnostic = analyze(campaign[0])["diagnostic_validity"]
    assert diagnostic["valid"] is True
    correlation = next(item for item in diagnostic["correlations"] if item["request_id"] == row["request_id"])
    assert "touching-nuisance" not in correlation["overlap_event_ids"]
    assert "background-nuisance" in correlation["overlap_event_ids"]


def test_malformed_memory_json_discards_ram_conclusions(campaign):
    write_resource_campaign(campaign)
    directory = campaign[0]
    with (directory / "memory.jsonl").open("a") as handle:
        handle.write("{invalid json}\n")
    original = (directory / "memory.jsonl").read_bytes()
    result = analyze(directory)
    assert result["valid"] is True
    assert result["overall_valid"] is False
    assert result["resource_validity"]["valid"] is False
    assert result["resource_validity"]["contrasts"] is None
    assert result["tradeoffs"] == {}
    assert not result["has_tradeoff_figure"]
    assert "| 11 - 10 | after_idle | rss |" not in (directory / "report.md").read_text()
    assert not (directory / "tradeoffs.png").exists()
    assert (directory / "memory.jsonl").read_bytes() == original


@pytest.mark.parametrize("experiment_id", [None, "", " ", 1, []])
def test_missing_or_malformed_experiment_identity_cannot_validate_matching_memory(campaign, experiment_id):
    records = write_resource_campaign(campaign)
    campaign[1]["experiment_id"] = experiment_id
    for record in records:
        record["experiment_id"] = experiment_id
    write_campaign(campaign)
    (campaign[0] / "memory.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    result = analyze(campaign[0])
    assert result["valid"] is False
    assert result["overall_valid"] is False
    assert result["resource_validity"]["valid"] is False
    assert result["contrasts"] is None
    assert result["resource_validity"]["contrasts"] is None


@pytest.mark.parametrize("source", [{"unverified": True}, {"sha256": None}, {"sha256": "x" * 64}, {"sha256": []}])
def test_source_without_valid_frozen_fingerprint_blocks_conclusions(campaign, source):
    campaign[1]["source"] = source
    result = analyze(write_campaign(campaign))
    assert result["valid"] is False
    assert result["overall_valid"] is False
    assert result["contrasts"] is None
    assert "INVALID" in (campaign[0] / "report.html").read_text()


@pytest.mark.parametrize("mutation", ["collapsed_setup", "moved_setup", "moved_body", "duplicated_body"])
def test_diagnostic_boundaries_must_match_primary_method_evidence(campaign, mutation):
    sequential_method_spans(campaign[2])
    events, metadata = write_diagnostics(campaign)
    row = next(row for row in campaign[2] if row["phase"] == "measured")
    selected = [event for event in events if event["request_id"] == row["request_id"]]
    setup = next(event for event in selected if event["name"] == "pre_component")
    body = next(event for event in selected if event["name"] == "component_body")
    if mutation == "collapsed_setup":
        setup["end_ns"] = setup["start_ns"]
    elif mutation == "moved_setup":
        setup["start_ns"] += 1000000
    elif mutation == "moved_body":
        body["start_ns"] += 1000000
        body["end_ns"] += 1000000
    else:
        events.append({**body, "event_id": "duplicate-method-body"})
        item = next(item for item in metadata if (item["block"], item["arm"]) == (row["block"], row["arm"]))
        item["metadata"]["drained"] += 1
        item["metadata"]["total_drained"] += 1
    save_diagnostic_fixture(campaign[0], events, metadata)
    result = analyze(campaign[0])
    assert result["valid"] is True
    diagnostic = result["diagnostic_validity"]
    assert diagnostic["valid"] is False
    assert result["overall_valid"] is False
    assert diagnostic["phase_summary"] == {}
    assert diagnostic["phase_summary_by_arm"] == {}
    assert diagnostic["phase_summary_by_block"] == {}
    assert "setup/pre_component" not in (campaign[0] / "report.html").read_text()


def test_matching_repeated_component_methods_keep_valid_union_statistics(campaign):
    row = next(row for row in campaign[2] if row["phase"] == "measured")
    row["component_intervals_ms"].append({"node_id": "load", "start_ms": 2, "end_ms": 8})
    write_diagnostics(campaign)
    result = analyze(campaign[0])
    assert result["overall_valid"] is True
    assert result["diagnostic_validity"]["phase_summary"]["component/component_body"]["mean"] == 40


@pytest.mark.parametrize("field", ["rss", "cache_accounting"])
@pytest.mark.parametrize("duration", [1e300, 10**400])
def test_out_of_range_memory_duration_writes_invalid_report(campaign, field, duration):
    records = write_resource_campaign(campaign)
    records[0][field]["duration_ms"] = duration
    (campaign[0] / "memory.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    result = analyze(campaign[0])
    assert result["valid"] is True
    assert result["overall_valid"] is False
    assert result["resource_validity"]["valid"] is False
    assert result["resource_validity"]["contrasts"] is None
    assert "INVALID" in (campaign[0] / "report.html").read_text()


@pytest.mark.parametrize("mutation", ["missing_telemetry", "malformed_effective"])
def test_malformed_controlled_profile_writes_invalid_report(campaign, mutation):
    write_resource_campaign(campaign)
    if mutation == "missing_telemetry":
        campaign[1]["profile"].pop("product_telemetry")
    else:
        campaign[3][0]["after_warmup"]["effective"] = []
    write_campaign(campaign)
    result = analyze(campaign[0])
    assert result["valid"] is False
    assert result["overall_valid"] is False
    assert result["contrasts"] is None
    assert "INVALID" in (campaign[0] / "report.html").read_text()
    assert json.loads((campaign[0] / "analysis.json").read_text())["overall_valid"] is False


def test_unrepresentable_primary_integer_writes_invalid_report(campaign):
    row = next(row for row in campaign[2] if row["phase"] == "measured")
    row["langflow_overhead_ms"] = 10**400
    result = analyze(write_campaign(campaign))
    assert result["valid"] is False
    assert result["overall_valid"] is False
    assert result["contrasts"] is None
    assert "INVALID" in (campaign[0] / "report.html").read_text()
