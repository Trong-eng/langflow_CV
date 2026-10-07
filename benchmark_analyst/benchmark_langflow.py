"""Prepare, run and analyze compilation-cache × warm-graph SCRFD campaigns."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from uuid import uuid4

from benchmark_analyst.protocol import (
    append_jsonl,
    file_digest,
    flow_digest,
    make_schedule,
    source_identity,
    utc_now,
    validate_model_binding,
    write_json,
)
from benchmark_analyst.runtime import Worker, cleanup_inputs, read_config, run_sample

ROOT = Path(__file__).resolve().parents[1]


def verify_workload(worker: Worker, session, config: dict, *, expected_digest: str | None = None) -> dict:
    exported = json.loads(Path(config["flow_export_path"]).read_text())
    response = session.get(f"{worker.base_url}/api/v1/flows/{config['flow_id']}", timeout=15)
    response.raise_for_status()
    flow = response.json()
    if flow_digest(flow) != flow_digest(exported):
        raise ValueError("saved flow differs from frozen flow export; export deliberately before measuring")
    if expected_digest is not None and flow_digest(flow) != expected_digest:
        raise ValueError("saved flow and export differ from the campaign manifest")
    ids = {node["id"] for node in flow["data"]["nodes"]}
    expected = {config["input_node_id"], config["output_node_id"], *config["scrfd_node_ids"]}
    if ids != expected:
        raise ValueError("flow nodes do not match the six-node SCRFD workload")
    validate_model_binding(flow, config)
    return flow


def prepare(args) -> None:
    config = read_config(args.config)
    if args.output.exists():
        raise ValueError("output directory already exists; choose a new directory")
    args.output.mkdir(parents=True)
    worker = Worker(ROOT, args.port, env_file=args.env_file, credentials_file=args.credentials_file)
    try:
        worker.start("00", args.output)
        with worker.session() as session:
            verify_workload(worker, session, config)
            row = run_sample(
                session,
                worker.base_url,
                config,
                Path(config["image_path"]).read_bytes(),
                Path(config["image_path"]).name,
                worker.token,
                block=0,
                arm="00",
                phase="reference",
                index=0,
                session_id=f"benchmark-reference-{uuid4()}",
                reference_path=args.output / "reference.jpg",
            )
            write_json(args.output / "reference_request.json", row)
            if row["outcome"] != "success":
                raise RuntimeError(f"reference failed: {row['error']}")
            config["reference_pixel_sha256"] = row["image_pixel_sha256"]
            write_json(args.output / "config.json", config)
            clean = cleanup_inputs(session, worker.base_url, config["flow_id"], [row["uploaded_file_path"]])
            write_json(args.output / "cleanup.json", {"files": clean})
            print(f"Reference ready: {args.output / 'reference.jpg'}", flush=True)
    finally:
        worker.stop()


def run(args) -> dict:
    if getattr(args, "fixture", None) is not None:
        from benchmark_analyst.campaigns import run_controlled

        return run_controlled(args)
    if getattr(args, "memory_checkpoints", False):
        raise ValueError("--memory-checkpoints requires an isolated --fixture")
    config = read_config(args.config)
    if not config.get("reference_pixel_sha256"):
        raise ValueError("prepare a reference first; config requires reference_pixel_sha256")
    if args.warmups < 1:
        raise ValueError("warmups must be positive")
    schedule = make_schedule(args.requests_per_arm, args.blocks)
    if args.requests_per_arm >= 1000:
        if not args.smoke:
            raise ValueError("main requires --smoke pointing to a successful smoke experiment")
    if args.output.exists():
        raise ValueError("output directory already exists; campaigns never overwrite or silently resume")
    image = Path(config["image_path"]).read_bytes()
    exported = json.loads(Path(config["flow_export_path"]).read_text())
    source = source_identity(ROOT)
    workload = {
        **config,
        "flow_sha256": flow_digest(exported),
        "input_sha256": file_digest(Path(config["image_path"])),
        "model_sha256": file_digest(Path(config["model_path"])),
    }
    if args.requests_per_arm >= 1000:
        from benchmark_analyst.campaign_contract import check_smoke

        check_smoke(args.smoke, {"workload": workload, "source": source, "warmups": args.warmups})
    worker = Worker(ROOT, args.port, env_file=args.env_file, credentials_file=args.credentials_file)
    if not worker.api_key:
        raise ValueError("LANGFLOW_API_KEY is missing")
    args.output.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "experiment_id": args.output.name,
        "created_at": utc_now(),
        "requests_per_arm": args.requests_per_arm,
        "blocks": args.blocks,
        "warmups": args.warmups,
        "primary_metric": "langflow_overhead_ms",
        "schedule": schedule,
        "workload": workload,
        "source": source,
        "runtime": {
            "platform": platform.platform(),
            "python": sys.version,
            "workers": 1,
            "concurrency": 1,
            "stream": False,
            "output_type": "chat",
            "native_tracing": True,
            "base_url": worker.base_url,
        },
        "measurement_boundary": (
            "Outer application ASGI entry through final response body send, including all Langflow middleware "
            "and HTTP telemetry, excluding post-response background work, minus union of SCRFD output method bodies"
        ),
        "smoke_directory": str(args.smoke) if args.smoke else None,
    }
    write_json(args.output / "manifest.json", manifest)
    write_json(args.output / "state.json", {"status": "RUNNING", "started_at": utc_now()})
    seen_images = set()
    try:
        for slot_index, slot in enumerate(schedule):
            block, arm = slot["block"], slot["arm"]
            if source_identity(ROOT)["sha256"] != source["sha256"]:
                raise RuntimeError("source changed during campaign")
            if file_digest(Path(config["model_path"])) != workload["model_sha256"]:
                raise RuntimeError("model changed during campaign")
            print(f"[{slot_index + 1}/{len(schedule)}] block={block} arm={arm}: starting fresh worker", flush=True)
            worker.start(arm, args.output / "logs" / f"b{block:02d}_{arm}")
            try:
                with worker.session() as session:
                    verify_workload(worker, session, config, expected_digest=workload["flow_sha256"])
                    evidence = {**slot, "before_warmup": worker.snapshot(session)}
                    cleanup_paths = []
                    session_id = f"benchmark-{args.output.name}-b{block}-{arm}-{uuid4()}"
                    for phase, count in [("warmup", args.warmups), ("measured", slot["count"])]:
                        for index in range(count):
                            row = run_sample(
                                session,
                                worker.base_url,
                                config,
                                image,
                                Path(config["image_path"]).name,
                                worker.token,
                                block=block,
                                arm=arm,
                                phase=phase,
                                index=index,
                                session_id=session_id,
                            )
                            if row.get("output_image_path"):
                                if row["output_image_path"] in seen_images:
                                    row.update(outcome="invalid", output_valid=False, error="output image URL reused")
                                seen_images.add(row["output_image_path"])
                            append_jsonl(args.output / "requests.jsonl", row)
                            if row.get("uploaded_file_path"):
                                cleanup_paths.append(row["uploaded_file_path"])
                            if row["outcome"] != "success":
                                print(f"  {phase}[{index}]: {row['outcome']}: {row['error']}", flush=True)
                            elif phase == "measured" and (index + 1) % 50 == 0:
                                print(
                                    f"  measured {index + 1}/{count}; overhead={row['langflow_overhead_ms']:.2f} ms",
                                    flush=True,
                                )
                        evidence["after_warmup" if phase == "warmup" else "after_measurement"] = worker.snapshot(
                            session
                        )
                    append_jsonl(args.output / "worker_evidence.jsonl", evidence)
                    verify_workload(worker, session, config, expected_digest=workload["flow_sha256"])
                    if file_digest(Path(config["model_path"])) != workload["model_sha256"]:
                        raise RuntimeError("model changed during slot")
                    for result in cleanup_inputs(session, worker.base_url, config["flow_id"], cleanup_paths):
                        append_jsonl(args.output / "cleanup.jsonl", {"block": block, "arm": arm, **result})
            finally:
                worker.stop()
        if source_identity(ROOT)["sha256"] != source["sha256"]:
            raise RuntimeError("source changed during campaign")
        write_json(args.output / "state.json", {"status": "COMPLETE", "finished_at": utc_now()})
    except BaseException as exc:
        write_json(
            args.output / "state.json",
            {"status": "INCOMPLETE", "finished_at": utc_now(), "error": f"{type(exc).__name__}: {exc}"},
        )
        raise
    finally:
        worker.stop()
    from benchmark_analyst.reporting import analyze

    result = analyze(args.output)
    print(f"{'VALID' if result['valid'] else 'INVALID'}: {args.output / 'report.html'}", flush=True)
    return result


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run", "freeze", "diagnose"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--env-file", type=Path, default=ROOT / ".env")
        command.add_argument("--credentials-file", type=Path)
        command.add_argument("--port", type=int, default=7860)
        if name in ("run", "diagnose"):
            command.add_argument("--requests-per-arm", type=int, default=1000)
            command.add_argument("--blocks", type=int, default=4)
            command.add_argument("--warmups", type=int, default=5)
            command.add_argument("--smoke", type=Path)
            command.add_argument("--fixture", type=Path, required=name == "diagnose")
            command.add_argument("--memory-checkpoints", action="store_true")
            command.add_argument("--keep-slot-data", action="store_true")
        if name == "freeze":
            command.add_argument("--source-database", type=Path)
            command.add_argument("--source-storage", type=Path)
    for name in ("analyze", "analyze-suite"):
        analysis = commands.add_parser(name)
        analysis.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not args.output.is_relative_to(ROOT / "benchmark_analyst" / "runs"):
        parser.error("--output must be under benchmark_analyst/runs (ignored, not source)")
    if args.command == "prepare":
        prepare(args)
    elif args.command == "run":
        result = run(args)
        return 0 if result.get("overall_valid", result["valid"]) else 2
    elif args.command == "freeze":
        from benchmark_analyst.campaigns import freeze_fixture

        freeze_fixture(args)
    elif args.command == "diagnose":
        from benchmark_analyst.campaigns import diagnose

        return 0 if diagnose(args)["overall_valid"] else 2
    elif args.command == "analyze-suite":
        from benchmark_analyst.suite_reporting import analyze_suite

        return 0 if analyze_suite(args.output)["overall_valid"] else 2
    else:
        from benchmark_analyst.reporting import analyze

        result = analyze(args.output)
        return 0 if result.get("overall_valid", result["valid"]) else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
