"""Reports retain actual latency, block replication and adverse evidence."""
# ruff: noqa: S101, PLR2004, INP001

import hashlib
import importlib
import json
from pathlib import Path

import pytest


def module():
    return importlib.import_module("benchmark_analyst.revision_reporting")


def dataset(*, adverse=False, invalid=False):
    manifest = {
        "experiment_id": "controlled-campaign",
        "mode": "campaign",
        "groups": {
            "MAIN": {"root": "/baseline", "source": {"git_revision": "a" * 40, "sha256": "a" * 64}},
            "COMPILE": {"root": "/compile", "source": {"git_revision": "a" * 40, "sha256": "b" * 64}},
        },
        "source": {"sha256": "c" * 64},
        "workload": {"flow_sha256": "d" * 64, "input_sha256": "e" * 64, "model_sha256": "f" * 64},
        "profile_id": "profile-frozen",
        "fixture_sha256": "0" * 64,
        "blocks": 4,
        "requests_per_arm": 8,
        "warmups": 5,
    }
    rows, evidence, memory = [], [], []
    for block in range(1, 5):
        for arm in ("MAIN", "COMPILE"):
            values = [100, 200] if arm == "MAIN" else ([110, 220] if adverse else [90, 180])
            for index, value in enumerate(values):
                server = value + block - 1
                rows.append(
                    {
                        "arm": arm,
                        "block": block,
                        "phase": "measured",
                        "index": index,
                        "server_total_ms": server,
                        "flow_api_ms": server + 10,
                        "langflow_overhead_ms": server - 80,
                        "scrfd_processing_ms": 80,
                        "outcome": "success",
                        "output_valid": True,
                    }
                )
            for index in range(5):
                rows.append(
                    {
                        "arm": arm,
                        "block": block,
                        "phase": "warmup",
                        "index": index,
                        "server_total_ms": 9000,
                        "flow_api_ms": 9010,
                        "langflow_overhead_ms": 8920,
                        "outcome": "success",
                        "output_valid": True,
                    }
                )
            evidence.append(
                {
                    "arm": arm,
                    "block": block,
                    "compilation_delta": None if arm == "MAIN" else {"hits": 20},
                    "before_warmup": {"compilation": None if arm == "MAIN" else {"hits": 0}},
                    "compilation_status": {"status": "unavailable", "reason": "absent_in_baseline"},
                }
            )
            for checkpoint in ("before_warmup", "after_warmup", "after_measurement", "after_idle"):
                memory.append(
                    {
                        "arm": arm,
                        "block": block,
                        "checkpoint": checkpoint,
                        "pid": block * 100 + (1 if arm == "MAIN" else 2),
                        "process_create_time": 1000.0,
                        "rss": {
                            "bytes": (100 + block + (20 if arm == "COMPILE" else 0)) * 1024 * 1024,
                            "status": "measured",
                            "reason": None,
                        },
                        "uss": {"bytes": None, "status": "unavailable", "reason": "AccessDenied"},
                    }
                )
    validation = {
        "overall_valid": not invalid,
        "valid": not invalid,
        "errors": ["output mismatch"] if invalid else [],
        "raw_sha256": {},
    }
    return manifest, rows, evidence, memory, validation


def row_matching(rows, **selectors):
    return next(row for row in rows if all(row[key] == value for key, value in selectors.items()))


def test_measured_latency_excludes_warmup_and_interpolates_descriptive_percentiles():
    # Including warmup or treating overhead as actual latency breaks these hand-derived values.
    analysis = module().build_analysis(*dataset())
    row = row_matching(analysis["summary"], arm="MAIN", block="all", metric="server_total_ms")
    assert row["n"] == 8
    assert row["mean"] == 151.5
    assert row["p50"] == 151.5
    assert row["p95"] == pytest.approx(202.65)
    assert row["p99"] == pytest.approx(202.93)
    assert analysis["replication"]["units_per_campaign"] == 4
    assert analysis["primary_metrics"] == ["server_total_ms", "flow_api_ms"]
    warmup = row_matching(analysis["warmup"], arm="MAIN", block=1, metric="flow_api_ms")
    assert warmup["n"] == 5
    assert warmup["total_ms"] == 45050


def test_compile_minus_main_is_signed_and_percentage_uses_main_denominator():
    analysis = module().build_analysis(*dataset())
    row = row_matching(analysis["comparisons"], block=1, metric="server_total_ms", statistic="mean")
    assert row["main"] == 150
    assert row["compile"] == 135
    assert row["delta_ms"] == -15
    assert row["delta_percent"] == -10
    assert row_matching(analysis["comparisons"], block="all", metric="server_total_ms", statistic="mean")[
        "delta_percent"
    ] == pytest.approx(-9.900990099)
    adverse = module().build_analysis(*dataset(adverse=True))
    row = row_matching(adverse["comparisons"], block=1, metric="server_total_ms", statistic="p99")
    assert row["delta_ms"] == pytest.approx(19.9)
    assert row["delta_percent"] == pytest.approx(10)


def test_invalid_outcome_is_preserved_and_blocks_latency_recommendation():
    args = dataset(invalid=True)
    args[1][0].update(outcome="output_mismatch", output_valid=False)
    analysis = module().build_analysis(*args)
    assert analysis["status"] == "INVALID"
    assert analysis["recommendation"]["supported"] is False
    assert "output mismatch" in analysis["validation"]["errors"]
    assert analysis["outcomes"]["MAIN"]["measured"]["attempted"] == 8
    assert analysis["outcomes"]["MAIN"]["measured"]["failed"] == 1
    assert analysis["outcomes"]["MAIN"]["measured"]["counts"]["output_mismatch"] == 1


def test_uss_unavailability_is_null_with_reason_and_rss_is_worker_memory():
    analysis = module().build_analysis(*dataset())
    uss = row_matching(analysis["resources"], arm="MAIN", block="all", checkpoint="after_idle", metric="uss")
    assert uss["mean_bytes"] is None
    assert uss["n"] == 0
    assert uss["unavailable"] == 4
    assert uss["reasons"] == {"AccessDenied": 4}
    rss = row_matching(analysis["resources"], arm="MAIN", block="all", checkpoint="after_idle", metric="rss")
    assert rss["mean_bytes"] == 102.5 * 1024 * 1024
    assert analysis["compilation"]["MAIN"]["status"] == "unavailable"
    assert "whole worker" in analysis["boundaries"]["memory"]


def write_raw(directory, *, adverse=False):
    directory.mkdir()
    manifest, rows, evidence, memory, validation = dataset(adverse=adverse)
    manifest["experiment_id"] = directory.name
    generation = float(int(hashlib.sha256(str(directory).encode()).hexdigest()[:8], 16))
    for row in memory:
        row["process_create_time"] = generation
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "state.json").write_text('{"status": "COMPLETE"}', encoding="utf-8")
    for filename, values in (("requests.jsonl", rows), ("worker_evidence.jsonl", evidence), ("memory.jsonl", memory)):
        (directory / filename).write_text("".join(json.dumps(row) + "\n" for row in values), encoding="utf-8")
    validation["raw_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()
    }
    return validation


def test_reports_have_usable_links_csv_png_and_preserve_raw(tmp_path, monkeypatch):
    directory = tmp_path / "campaign"
    validation = write_raw(directory)
    monkeypatch.setattr(module(), "_validate", lambda _: validation)
    before = {name: (directory / name).read_bytes() for name in validation["raw_sha256"]}
    result = module().report_run(directory)
    assert result["status"] == "VALID"
    receipt = json.loads((directory / "report_verification.json").read_text())
    assert receipt["links_valid"] is True
    assert receipt["raw_unchanged"] is True
    for name in ("charts.png", "tradeoffs.png"):
        assert receipt["png_dimensions"][name][0] > 500
        assert (directory / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    text = (directory / "report.html").read_text()
    assert 'href="manifest.json"' in text
    assert 'href="resource_summary.csv"' in text
    assert all((directory / name).read_bytes() == value for name, value in before.items())
    with pytest.raises(FileExistsError):
        module().report_run(directory)


def test_repeat_disagreement_prevents_positive_latency_recommendation(tmp_path, monkeypatch):
    first, repeat, output = tmp_path / "first", tmp_path / "repeat", tmp_path / "combined"
    validations = {first: write_raw(first), repeat: write_raw(repeat, adverse=True)}
    monkeypatch.setattr(module(), "_validate", lambda path: validations[Path(path)])
    module().report_run(first)
    module().report_run(repeat)
    result = module().compare_campaigns(first, repeat, output)
    assert result["status"] == "VALID"
    assert result["consistency"]["server_total_ms"]["direction_consistent"] is False
    assert result["recommendation"]["worth_keeping_for_latency"] is False
    assert result["recommendation"]["rss_delta_after_idle_mib"] == [20, 20]
    assert result["runs"][0]["raw_sha256"]["manifest.json"] == validations[first]["raw_sha256"]["manifest.json"]
    receipt = json.loads((output / "verification_receipt.json").read_text())
    assert receipt["links_valid"] is True
    with pytest.raises(FileExistsError):
        module().compare_campaigns(first, repeat, output)


@pytest.mark.parametrize("corruption", ["analysis", "provenance", "worker_reuse"])
def test_repeat_rejects_stale_metrics_changed_harness_and_reused_workers(tmp_path, monkeypatch, corruption):
    first, repeat = tmp_path / "first", tmp_path / "repeat"
    validations = {first: write_raw(first), repeat: write_raw(repeat)}
    monkeypatch.setattr(module(), "_validate", lambda path: validations[Path(path)])
    module().report_run(first)
    module().report_run(repeat)
    if corruption == "analysis":
        path = repeat / "analysis.json"
        value = json.loads(path.read_text())
        for row in value["comparisons"]:
            row["delta_ms"] = -100000
        path.write_text(json.dumps(value))
    elif corruption == "provenance":
        path = repeat / "manifest.json"
        value = json.loads(path.read_text())
        value["source"]["sha256"] = "9" * 64
        path.write_text(json.dumps(value))
    else:
        (repeat / "memory.jsonl").write_bytes((first / "memory.jsonl").read_bytes())
    result = module().compare_campaigns(first, repeat, tmp_path / "combined")
    assert result["status"] == "INVALID"
    assert result["recommendation"]["worth_keeping_for_latency"] is None
    assert result["consistency"]["server_total_ms"]["mean_delta_ms"] == [-15, -15]


def test_consistent_actual_latency_benefit_and_ram_cost_support_empirical_retention(tmp_path, monkeypatch):
    first, repeat = tmp_path / "first", tmp_path / "repeat"
    validations = {first: write_raw(first), repeat: write_raw(repeat)}
    monkeypatch.setattr(module(), "_validate", lambda path: validations[Path(path)])
    module().report_run(first)
    module().report_run(repeat)
    result = module().compare_campaigns(first, repeat, tmp_path / "combined")
    assert result["status"] == "VALID"
    assert result["recommendation"]["worth_keeping_for_latency"] is True
    assert result["consistency"]["flow_api_ms"]["favorable_blocks"] == [4, 4]
    assert result["recommendation"]["rss_delta_after_idle_mib"] == [20, 20]


def test_real_validator_handoff_preserves_gate_and_reports_invalid_attempt(tmp_path):
    directory = tmp_path / "failed-attempt"
    write_raw(directory)
    from benchmark_analyst.revision_campaign import validate_run

    gate = validate_run(directory)
    assert gate["overall_valid"] is False
    (directory / "validation.json").write_text(json.dumps(gate))
    original = (directory / "validation.json").read_bytes()
    analysis = module().report_run(directory)
    assert analysis["status"] == "INVALID"
    assert analysis["recommendation"]["supported"] is False
    assert (directory / "validation.json").read_bytes() == original
    assert 'href="validation.json"' in (directory / "report.html").read_text()
