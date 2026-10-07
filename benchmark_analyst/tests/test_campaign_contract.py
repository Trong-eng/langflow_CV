"""Treatment schedules and smoke gates must verify the actual experiment."""

import hashlib
import json
import subprocess

import pytest


def test_diagnostic_schedule_balances_variants_and_pairs():
    from benchmark_analyst.protocol import make_diagnostic_schedule

    schedule = make_diagnostic_schedule(1000, 4)
    assert len(schedule) == 32
    for variant in ("D0", "D1", "D2", "D3"):
        rows = [r for r in schedule if r["variant"] == variant]
        assert sum(r["count"] for r in rows if r["arm"] == "10") == 1000
        assert [r["arm"] for r in rows] == ["10", "11", "11", "10", "11", "10", "10", "11"]
    for position in range(4):
        assert {schedule[(block * 8) + position * 2]["variant"] for block in range(4)} == {"D0", "D1", "D2", "D3"}


def test_execution_fingerprint_excludes_tracked_runs(tmp_path):
    from benchmark_analyst.protocol import source_identity

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    # A local identity is only required by the temporary test repository.
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    source = tmp_path / "benchmark_analyst" / "observer.py"
    artifact = tmp_path / "benchmark_analyst" / "runs" / "tracked" / "report.html"
    artifact.parent.mkdir(parents=True)
    source.write_text("source = 1\n")
    artifact.write_text("old")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    before = source_identity(tmp_path)["sha256"]
    artifact.write_text("new")
    assert source_identity(tmp_path)["sha256"] == before
    source.write_text("source = 2\n")
    assert source_identity(tmp_path)["sha256"] != before


def test_smoke_requires_resource_and_effective_profile_match(tmp_path):
    from benchmark_analyst.campaign_contract import check_smoke

    manifest = {
        "kind": "factorial",
        "workload": {"a": 1},
        "source": {"sha256": "x"},
        "warmups": 5,
        "profile_id": "profile",
        "fixture_sha256": "fixture",
        "resource_measurement": {"enabled": True},
        "diagnostics": {"enabled": False},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for name in ("requests.jsonl", "worker_evidence.jsonl", "memory.jsonl"):
        (tmp_path / name).write_text("{}\n")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    (tmp_path / "analysis.json").write_text(json.dumps({"valid": True, "overall_valid": True, "raw_sha256": hashes}))
    check_smoke(tmp_path, manifest)
    for field in ["profile_id", "fixture_sha256", "resource_measurement", "diagnostics"]:
        changed = {**manifest, field: "different"}
        with pytest.raises(ValueError, match="smoke"):
            check_smoke(tmp_path, changed)
    (tmp_path / "memory.jsonl").write_text("changed")
    with pytest.raises(ValueError, match="smoke raw"):
        check_smoke(tmp_path, manifest)
    (tmp_path / "analysis.json").write_text(json.dumps({"valid": True, "overall_valid": False}))
    with pytest.raises(ValueError, match="smoke"):
        check_smoke(tmp_path, manifest)


@pytest.mark.parametrize(
    "modern,overall,accepted", [(True, None, False), (True, 1, False), (True, True, True), (False, None, True)]
)
def test_only_genuine_legacy_smoke_can_fall_back_to_latency_status(tmp_path, modern, overall, accepted):
    from benchmark_analyst.campaign_contract import check_smoke

    manifest = {"workload": {"a": 1}, "source": {"sha256": "x"}, "warmups": 5}
    if modern:
        manifest.update(fixture_sha256="fixture", profile_id="profile", resource_measurement={"enabled": True})
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for name in ("requests.jsonl", "worker_evidence.jsonl", "memory.jsonl"):
        (tmp_path / name).write_text("{}\n")
    analysis = {
        "valid": True,
        "raw_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()},
    }
    if overall is not None:
        analysis["overall_valid"] = overall
    (tmp_path / "analysis.json").write_text(json.dumps(analysis))
    if accepted:
        check_smoke(tmp_path, manifest)
    else:
        with pytest.raises(ValueError, match="smoke"):
            check_smoke(tmp_path, manifest)


def test_cli_exposes_fixture_memory_and_diagnostic_suite():
    from benchmark_analyst.benchmark_langflow import make_parser

    parser = make_parser()
    args = parser.parse_args(
        [
            "run",
            "--config",
            "config.json",
            "--output",
            "runs/smoke",
            "--fixture",
            "runs/baseline",
            "--memory-checkpoints",
        ]
    )
    assert args.memory_checkpoints and str(args.fixture) == "runs/baseline"
    args = parser.parse_args(
        [
            "diagnose",
            "--config",
            "config.json",
            "--output",
            "runs/diag",
            "--fixture",
            "runs/baseline",
            "--smoke",
            "runs/diag-smoke",
        ]
    )
    assert args.requests_per_arm == 1000 and args.blocks == 4
    args = parser.parse_args(["freeze", "--config", "config.json", "--output", "runs/baseline"])
    assert args.command == "freeze"


def test_profile_identity_includes_effective_settings_without_slot_paths():
    from benchmark_analyst.campaign_contract import profile_identity

    common = {"gc_thresholds": [700, 10, 10], "versions": {"python": "3.13"}}
    kwargs = {"diagnostics": False, "telemetry": True, "warmups": 5}
    first, descriptor = profile_identity("fixture", effective_common_settings=common, **kwargs)
    changed, _ = profile_identity(
        "fixture", effective_common_settings={**common, "gc_thresholds": [500, 10, 10]}, **kwargs
    )
    assert first != changed
    assert descriptor["effective_common_settings"] == common


def test_effective_profile_is_bound_once_and_verified_offline():
    from benchmark_analyst.campaign_contract import (
        bind_effective_profile,
        observed_settings,
        profile_identity,
        validate_effective_profile,
    )

    identifier, profile = profile_identity("fixture", diagnostics=False, telemetry=True, warmups=5)
    manifest = {"fixture_sha256": "fixture", "profile_id": identifier, "profile": profile}
    snapshot = {
        "effective": {
            "gc_thresholds": [700, 10, 10],
            "gc_enabled": True,
            "native_tracing": True,
            "product_telemetry_enabled": True,
            "uvicorn_access_enabled": False,
        }
    }
    common = observed_settings(snapshot)
    bind_effective_profile(manifest, common)
    assert manifest["profile_id"] != identifier
    evidence = [
        {
            "block": 1,
            "arm": "10",
            **{name: snapshot for name in ("before_warmup", "after_warmup", "after_measurement", "after_idle")},
        }
    ]
    errors = []
    validate_effective_profile(manifest, evidence, errors)
    assert not errors
    with pytest.raises(ValueError, match="since smoke"):
        bind_effective_profile(manifest, {**common, "gc_thresholds": [500, 10, 10]})
    evidence[0]["after_idle"] = {"effective": {"gc_thresholds": [500, 10, 10]}}
    validate_effective_profile(manifest, evidence, errors)
    assert len(errors) == 1 and "after_idle" in errors[0]
    evidence[0]["after_idle"] = {"effective": {**snapshot["effective"], "product_telemetry_enabled": False}}
    errors = []
    validate_effective_profile(manifest, evidence, errors)
    assert len(errors) == 1 and "product_telemetry_enabled" in errors[0]


@pytest.mark.parametrize(
    "declaration",
    [
        {"fixture_sha256": "fixture"},
        {"resource_measurement": {"enabled": True}},
        {"diagnostics": {"enabled": True}},
        {"kind": "diagnostic"},
    ],
)
def test_declared_modern_evidence_cannot_be_downgraded_to_legacy_profile(declaration):
    from benchmark_analyst.campaign_contract import validate_effective_profile

    errors = []
    validate_effective_profile(declaration, [], errors)
    assert errors and "profile" in " ".join(errors)


@pytest.mark.parametrize(
    "mutation",
    [
        "profile",
        "profile_id",
        "telemetry",
        "workers",
        "common",
        "common_field",
        "numeric_bool",
        "effective_list",
        "effective_missing",
    ],
)
def test_malformed_controlled_profile_is_invalid_without_raising(mutation):
    from benchmark_analyst.campaign_contract import observed_settings, profile_identity, validate_effective_profile

    snapshot = {
        "effective": {
            "native_tracing": True,
            "gc_enabled": True,
            "product_telemetry_enabled": True,
            "uvicorn_access_enabled": False,
        }
    }
    identifier, profile = profile_identity(
        "fixture", diagnostics=False, telemetry=True, warmups=5, effective_common_settings=observed_settings(snapshot)
    )
    manifest = {
        "fixture_sha256": "fixture",
        "profile_id": identifier,
        "profile": profile,
        "warmups": 5,
        "diagnostics": {"enabled": False},
    }
    evidence = [
        {
            "block": 1,
            "arm": "10",
            **{name: snapshot for name in ("before_warmup", "after_warmup", "after_measurement", "after_idle")},
        }
    ]
    if mutation == "profile":
        manifest["profile"] = []
    elif mutation == "profile_id":
        del manifest["profile_id"]
    elif mutation == "telemetry":
        del profile["product_telemetry"]
    elif mutation == "workers":
        del profile["workers"]
    elif mutation == "common":
        profile["effective_common_settings"] = []
    elif mutation == "common_field":
        del profile["effective_common_settings"]["versions"]
    elif mutation == "numeric_bool":
        profile["gc_enabled"] = 1
    elif mutation == "effective_list":
        evidence[0]["after_idle"] = {"effective": []}
    elif mutation == "effective_missing":
        evidence[0]["after_idle"] = {"effective": {}}
    if mutation != "profile_id":
        manifest["profile_id"] = hashlib.sha256(json.dumps(manifest["profile"], sort_keys=True).encode()).hexdigest()
    errors = []
    validate_effective_profile(manifest, evidence, errors)
    assert errors


def test_genuine_legacy_evidence_does_not_require_effective_profile():
    from benchmark_analyst.campaign_contract import validate_effective_profile

    errors = []
    validate_effective_profile({"kind": "factorial"}, [{"before_warmup": {"pid": 1}}], errors)
    assert errors == []
