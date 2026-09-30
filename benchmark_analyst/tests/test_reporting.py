"""Offline report gates: incomplete or invalid experiments cannot claim speedup."""
# ruff: noqa: S101, PLR2004, INP001

import json

import pytest
from benchmark_analyst.reporting import analyze


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
        "source": {"git_head": "e" * 40, "file_sha256": {"observer.py": "f" * 64}},
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
