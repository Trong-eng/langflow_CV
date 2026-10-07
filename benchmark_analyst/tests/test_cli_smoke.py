"""Smoke authorization happens before a worker or output directory is created."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from benchmark_analyst import benchmark_langflow as cli
from benchmark_analyst.campaign_contract import check_smoke
from benchmark_analyst.protocol import file_digest, flow_digest


@pytest.fixture
def legacy_smoke(tmp_path, monkeypatch):
    for name, content in (
        ("flow.json", '{"data":{"nodes":[],"edges":[]}}'),
        ("image.jpg", "image"),
        ("model.onnx", "model"),
    ):
        (tmp_path / name).write_text(content)
    config = {
        "flow_id": "flow",
        "input_node_id": "input",
        "output_node_id": "output",
        "scrfd_node_ids": ["pre", "infer", "draw", "save"],
        "expected_faces": 1,
        "image_path": str(tmp_path / "image.jpg"),
        "model_path": str(tmp_path / "model.onnx"),
        "flow_export_path": str(tmp_path / "flow.json"),
        "reference_pixel_sha256": "a" * 64,
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    source = {"sha256": "f" * 64, "files": {"observer.py": "e" * 64}}
    monkeypatch.setattr(cli, "source_identity", lambda _: source)
    manifest = {
        "workload": {
            **config,
            "flow_sha256": flow_digest(json.loads((tmp_path / "flow.json").read_text())),
            "input_sha256": file_digest(tmp_path / "image.jpg"),
            "model_sha256": file_digest(tmp_path / "model.onnx"),
        },
        "source": source,
        "warmups": 5,
    }
    smoke = tmp_path / "smoke"
    smoke.mkdir()
    for name in ("requests.jsonl", "worker_evidence.jsonl"):
        (smoke / name).write_text("{}\n")
    args = SimpleNamespace(
        fixture=None,
        memory_checkpoints=False,
        config=tmp_path / "config.json",
        requests_per_arm=1000,
        blocks=4,
        warmups=5,
        smoke=smoke,
        output=tmp_path / "main",
        port=17869,
        env_file=None,
        credentials_file=None,
    )

    def save(*, overall_valid=True):
        (smoke / "manifest.json").write_text(json.dumps(manifest))
        hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in smoke.iterdir()
            if path.name != "analysis.json"
        }
        (smoke / "analysis.json").write_text(
            json.dumps({"valid": True, "overall_valid": overall_valid, "raw_sha256": hashes})
        )

    save()
    return args, manifest, save


@pytest.mark.parametrize(
    "mutation",
    [
        "controlled",
        "invalid_resources",
        "raw_drift",
        "deleted_state",
        "missing_raw_integrity",
        "profile",
        "source",
        "workload",
    ],
)
def test_legacy_run_rejects_incompatible_smoke_before_any_side_effect(legacy_smoke, monkeypatch, mutation):
    args, manifest, save = legacy_smoke
    if mutation in ("controlled", "invalid_resources"):
        manifest.update(
            fixture_sha256="fixture", profile_id="controlled-profile", resource_measurement={"enabled": True}
        )
        (args.smoke / "memory.jsonl").write_text("{}\n")
    elif mutation == "profile":
        manifest["profile_id"] = "unexpected-profile"
    elif mutation == "source":
        manifest["source"] = {"sha256": "0" * 64}
    elif mutation == "workload":
        manifest["workload"]["input_sha256"] = "0" * 64
    elif mutation == "deleted_state":
        (args.smoke / "state.json").write_text('{"status":"COMPLETE"}')
    save(overall_valid=mutation != "invalid_resources")
    if mutation == "raw_drift":
        (args.smoke / "requests.jsonl").write_text("changed\n")
    elif mutation == "deleted_state":
        (args.smoke / "state.json").unlink()
    elif mutation == "missing_raw_integrity":
        analysis = json.loads((args.smoke / "analysis.json").read_text())
        del analysis["raw_sha256"]
        (args.smoke / "analysis.json").write_text(json.dumps(analysis))

    def forbidden_worker(*_, **__):
        pytest.fail("smoke was accepted before worker construction")

    monkeypatch.setattr(cli, "Worker", forbidden_worker)
    with pytest.raises(ValueError, match="smoke"):
        cli.run(args)
    assert not args.output.exists()


def test_matching_genuine_legacy_smoke_remains_accepted(legacy_smoke):
    args, manifest, _ = legacy_smoke
    check_smoke(args.smoke, manifest)
