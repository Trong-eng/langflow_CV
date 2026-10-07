"""Controlled fixture campaigns and interleaved diagnostic suites.

The primary ASGI clock remains in the serving worker. Collectors, checkpoints,
drains and file verification run outside request timing, without adjusting it.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import sys
import time
from pathlib import Path
from uuid import uuid4

from benchmark_analyst.campaign_contract import (
    bind_effective_profile,
    check_smoke,
    diagnostics_declaration,
    observed_settings,
    profile_identity,
)
from benchmark_analyst.fixtures import LocalFixture, source_locations, verify_fixture_worker
from benchmark_analyst.memory_metrics import MemoryCollector, resource_declaration
from benchmark_analyst.protocol import (
    DIAGNOSTIC_VARIANTS,
    append_jsonl,
    file_digest,
    flow_digest,
    make_diagnostic_schedule,
    make_schedule,
    source_identity,
    utc_now,
    write_json,
)
from benchmark_analyst.runtime import Worker, cleanup_inputs, read_config, run_sample

ROOT = Path(__file__).resolve().parents[1]


def variant_settings(variant: str) -> dict:
    if variant not in DIAGNOSTIC_VARIANTS:
        raise ValueError("unknown diagnostic variant")
    return {"diagnostics": variant != "D0", "telemetry": variant != "D2", "warmups": 20 if variant == "D3" else 5}


def verify_profile_worker(snapshot: dict, *, telemetry: bool) -> None:
    effective = snapshot.get("effective", {})
    for key, expected in {
        "product_telemetry_enabled": telemetry,
        "native_tracing": True,
        "uvicorn_access_enabled": False,
        "gc_enabled": True,
    }.items():
        if effective.get(key) is not expected:
            raise ValueError(f"effective {key} does not match controlled profile")


def _host_context() -> dict:
    import psutil

    return {
        "platform": platform.platform(),
        "python": sys.version,
        "interpreter": sys.executable,
        "cpu_logical": os.cpu_count(),
        "cpu_physical": psutil.cpu_count(logical=False),
        "ram_total_bytes": psutil.virtual_memory().total,
        "ram_available_bytes": psutil.virtual_memory().available,
        "load_average": list(os.getloadavg()),
        "monotonic_resolution_seconds": time.get_clock_info("monotonic").resolution,
        "power_mode": {"status": "unavailable", "reason": "not_collected"},
        "runner_versions": {
            package: importlib.metadata.version(package)
            for package in ("psutil", "matplotlib", "pillow", "requests", "python-dotenv")
        },
    }


def freeze_fixture(args) -> dict:
    config = read_config(args.config)
    worker = Worker(ROOT, args.port, env_file=args.env_file, credentials_file=args.credentials_file)
    source_env = dict(worker.env)
    if args.source_database:
        source_env["LANGFLOW_DATABASE_URL"] = "sqlite:///" + str(args.source_database.resolve())
    if args.source_storage:
        source_env["LANGFLOW_CONFIG_DIR"] = str(args.source_storage.resolve())
    db, storage = source_locations(source_env, ROOT)
    fixture = LocalFixture.create(
        db, storage, args.output, config["flow_id"], request_file_overrides={(config["input_node_id"], "files")}
    )
    print(f"Frozen local fixture: {args.output}; sha256={fixture.sha256}", flush=True)
    return fixture.metadata


def _manifest(
    args,
    fixture: LocalFixture,
    *,
    directory: Path,
    schedule: list[dict],
    kind: str = "factorial",
    variant: str | None = None,
    source: dict | None = None,
) -> tuple[dict, dict, bytes]:
    config = read_config(args.config)
    if not config.get("reference_pixel_sha256"):
        raise ValueError("controlled campaign requires the frozen pixel reference")
    if args.blocks != 4:
        raise ValueError("controlled campaign requires four balanced blocks")
    if args.warmups < 1:
        raise ValueError("warmups must be positive")
    flags = variant_settings(variant) if variant else {"diagnostics": False, "telemetry": True, "warmups": args.warmups}
    effective_common_settings = None
    if args.smoke:
        smoke_directory = args.smoke / variant if variant else args.smoke
        previous = json.loads((smoke_directory / "manifest.json").read_text())
        effective_common_settings = previous.get("profile", {}).get("effective_common_settings")
        if not isinstance(effective_common_settings, dict):
            raise ValueError("smoke is missing frozen effective settings; rerun smoke")
    workload = {
        **config,
        "flow_sha256": flow_digest(json.loads(Path(config["flow_export_path"]).read_text())),
        "input_sha256": file_digest(Path(config["image_path"])),
        "model_sha256": file_digest(Path(config["model_path"])),
    }
    profile_id, profile = profile_identity(
        fixture.sha256,
        **{"diagnostics": flags["diagnostics"], "telemetry": flags["telemetry"], "warmups": flags["warmups"]},
        effective_common_settings=effective_common_settings,
    )
    manifest = {
        "schema_version": 1,
        "kind": kind,
        "mode": "campaign" if args.requests_per_arm >= 1000 else "smoke",
        "experiment_id": directory.name,
        "created_at": utc_now(),
        "requests_per_arm": args.requests_per_arm,
        "blocks": args.blocks,
        "warmups": flags["warmups"],
        "primary_metric": "langflow_overhead_ms",
        "schedule": schedule,
        "workload": workload,
        "source": source or source_identity(ROOT),
        "resource_measurement": resource_declaration(),
        "diagnostics": diagnostics_declaration(flags["diagnostics"]),
        "profile_id": profile_id,
        "profile": profile,
        "fixture_sha256": fixture.sha256,
        "runtime": {
            **_host_context(),
            "workers": 1,
            "concurrency": 1,
            "stream": False,
            "output_type": "chat",
            "native_tracing": True,
        },
        "measurement_boundary": "Outer application ASGI entry through final response body send, including all Langflow middleware and HTTP telemetry, excluding post-response background work, minus union of SCRFD output method bodies",
        "smoke_directory": str(args.smoke) if args.smoke else None,
    }
    if variant:
        manifest["variant"] = variant
    return manifest, config, Path(config["image_path"]).read_bytes()


def _initialize(directory: Path, manifest: dict) -> None:
    if directory.exists():
        raise ValueError("output directory exists; never overwrite or silently resume")
    directory.mkdir(parents=True)
    write_json(directory / "manifest.json", manifest)
    write_json(directory / "state.json", {"status": "RUNNING", "started_at": utc_now()})
    if manifest["diagnostics"]["enabled"]:
        (directory / "diagnostic_events.jsonl").touch()
        (directory / "diagnostic_metadata.jsonl").touch()


def _integrity(manifest: dict) -> None:
    if source_identity(ROOT)["sha256"] != manifest["source"]["sha256"]:
        raise RuntimeError("execution source changed during campaign")
    workload = manifest["workload"]
    for key, digest in (("model_path", "model_sha256"), ("image_path", "input_sha256")):
        if file_digest(Path(workload[key])) != workload[digest]:
            raise RuntimeError(f"frozen {key} changed during campaign")
    if flow_digest(json.loads(Path(workload["flow_export_path"]).read_text())) != workload["flow_sha256"]:
        raise RuntimeError("frozen flow export changed during campaign")


def _drain(worker: Worker, session, directory: Path, manifest: dict, slot: dict, phase: str) -> None:
    started = time.perf_counter()
    result = worker.drain_diagnostics(session, fresh_connection=phase == "final")
    identity = {"experiment_id": manifest["experiment_id"], "block": slot["block"], "arm": slot["arm"]}
    for event in result.get("events", []):
        append_jsonl(directory / "diagnostic_events.jsonl", {**event, **identity})
    append_jsonl(
        directory / "diagnostic_metadata.jsonl",
        {
            **identity,
            "phase": phase,
            "collected_at": utc_now(),
            "drain_duration_ms": (time.perf_counter() - started) * 1000,
            "metadata": result["metadata"],
        },
    )


def _light_snapshot(directory: Path, manifest: dict, slot: dict, pid: int, index: int) -> None:
    import psutil

    started = time.perf_counter()
    row = {
        "experiment_id": manifest["experiment_id"],
        "block": slot["block"],
        "arm": slot["arm"],
        "index": index,
        "collected_at": utc_now(),
        "pid": pid,
    }
    try:
        process = psutil.Process(pid)
        cpu = process.cpu_times()
        row.update(
            rss_bytes=process.memory_info().rss,
            process_threads=process.num_threads(),
            process_cpu_seconds=cpu.user + cpu.system,
            system_available_bytes=psutil.virtual_memory().available,
            status="measured",
        )
    except Exception as exc:
        row.update(status="unavailable", reason=type(exc).__name__)
    row["duration_ms"] = (time.perf_counter() - started) * 1000
    # These are process/system observations, not request-exclusive CPU or cache bytes.
    append_jsonl(directory / "diagnostic_snapshots.jsonl", row)


def _run_slot(
    args,
    fixture: LocalFixture,
    directory: Path,
    manifest: dict,
    config: dict,
    image: bytes,
    slot: dict,
    *,
    diagnostic_control: bool,
    seen_images: set[str],
    settings_guard: dict,
) -> None:
    from benchmark_analyst.benchmark_langflow import verify_workload

    _integrity(manifest)
    data = fixture.clone(directory / "slots" / f"b{slot['block']:02d}_{slot['arm']}")
    profile = manifest["profile"]
    worker = Worker(
        ROOT,
        args.port,
        env_file=args.env_file,
        credentials_file=args.credentials_file,
        fixture=data,
        controlled=True,
        diagnostics=profile["diagnostics_enabled"],
        telemetry_enabled=profile["product_telemetry"],
    )
    if not worker.api_key:
        raise ValueError("LANGFLOW_API_KEY is missing")
    success = False
    all_rows_successful = True
    print(f"{manifest['experiment_id']} block={slot['block']} arm={slot['arm']}: fresh isolated worker", flush=True)
    try:
        worker.start(slot["arm"], directory / "logs" / f"b{slot['block']:02d}_{slot['arm']}")
        with worker.session() as session:
            startup = worker.snapshot(session)
            verify_fixture_worker(startup, data)
            verify_profile_worker(startup, telemetry=profile["product_telemetry"])
            observed = observed_settings(startup)
            bind_effective_profile(manifest, observed)
            profile = manifest["profile"]
            write_json(directory / "manifest.json", manifest)
            if "baseline" in settings_guard and observed != settings_guard["baseline"]:
                raise ValueError("effective versions/GC/thread/exporter settings changed between slots")
            settings_guard.setdefault("baseline", observed)
            append_jsonl(
                directory / "effective_profiles.jsonl",
                {
                    "block": slot["block"],
                    "arm": slot["arm"],
                    "profile_id": manifest["profile_id"],
                    "observed_common_settings": observed,
                },
            )
            verify_workload(worker, session, config, expected_digest=manifest["workload"]["flow_sha256"])
            before = worker.snapshot(session)
            evidence = {
                **slot,
                "before_warmup": before,
                "fixture_sha256": fixture.sha256,
                "profile_id": manifest["profile_id"],
            }
            collector = MemoryCollector(manifest["experiment_id"], slot["block"], slot["arm"], before)
            append_jsonl(directory / "memory.jsonl", collector.collect("before_warmup", snapshot=before))
            if diagnostic_control:
                _drain(worker, session, directory, manifest, slot, "preflight")
            uploaded, last_sample_finished = [], None
            session_id = f"benchmark-{manifest['experiment_id']}-b{slot['block']}-{slot['arm']}-{uuid4()}"
            for phase, count in (("warmup", manifest["warmups"]), ("measured", slot["count"])):
                successes = 0
                for index in range(count):
                    row = run_sample(
                        session,
                        worker.base_url,
                        config,
                        image,
                        Path(config["image_path"]).name,
                        worker.token,
                        block=slot["block"],
                        arm=slot["arm"],
                        phase=phase,
                        index=index,
                        session_id=session_id,
                    )
                    last_sample_finished = time.monotonic()
                    path = row.get("output_image_path")
                    if path:
                        if path in seen_images:
                            row.update(outcome="invalid", output_valid=False, error="output image URL reused")
                        seen_images.add(path)
                    append_jsonl(directory / "requests.jsonl", row)
                    if row.get("uploaded_file_path"):
                        uploaded.append(row["uploaded_file_path"])
                    if row["outcome"] == "success":
                        successes += 1
                    else:
                        all_rows_successful = False
                        print(f"  {phase}[{index}]: {row['outcome']}: {row.get('error')}", flush=True)
                    if diagnostic_control:
                        _drain(worker, session, directory, manifest, slot, phase)
                        if phase == "measured" and (index + 1) % 25 == 0:
                            _light_snapshot(directory, manifest, slot, before["pid"], index)
                    if phase == "measured" and (index + 1) % 50 == 0:
                        print(f"  measured {index + 1}/{count}", flush=True)
                checkpoint = "after_warmup" if phase == "warmup" else "after_measurement"
                snapshot = worker.snapshot(session)
                verify_fixture_worker(snapshot, data)
                verify_profile_worker(snapshot, telemetry=profile["product_telemetry"])
                if observed_settings(snapshot) != settings_guard["baseline"]:
                    raise ValueError("effective settings changed during slot")
                evidence[checkpoint] = snapshot
                append_jsonl(
                    directory / "memory.jsonl",
                    collector.collect(checkpoint, snapshot=snapshot, last_sample_finished=last_sample_finished),
                )
                if phase == "warmup" and successes != count:
                    raise RuntimeError("warmup output failures; preserve raw and stop before long measurement")
            # No workload, cleanup or polls during this fixed minimum idle interval.
            remaining = last_sample_finished + 5 - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            after_idle = worker.snapshot(session, fresh_connection=True)
            verify_fixture_worker(after_idle, data)
            verify_profile_worker(after_idle, telemetry=profile["product_telemetry"])
            if observed_settings(after_idle) != settings_guard["baseline"]:
                raise ValueError("effective settings changed during idle interval")
            evidence["after_idle"] = after_idle
            append_jsonl(
                directory / "memory.jsonl",
                collector.collect("after_idle", snapshot=after_idle, last_sample_finished=last_sample_finished),
            )
            # Discard the measurement pool for every post-idle control operation.
            with worker.session() as final_session:
                if diagnostic_control:
                    _drain(worker, final_session, directory, manifest, slot, "final")
                append_jsonl(directory / "worker_evidence.jsonl", evidence)
                verify_workload(worker, final_session, config, expected_digest=manifest["workload"]["flow_sha256"])
                _integrity(manifest)
                for result in cleanup_inputs(final_session, worker.base_url, config["flow_id"], uploaded):
                    append_jsonl(directory / "cleanup.jsonl", {"block": slot["block"], "arm": slot["arm"], **result})
            success = all_rows_successful
    finally:
        worker.stop()
        if success and not args.keep_slot_data:
            fixture.release(data)
            append_jsonl(
                directory / "fixture_cleanup.jsonl",
                {"block": slot["block"], "arm": slot["arm"], "removed_owned_slot": True, "after_worker_stop": True},
            )


def run_controlled(args) -> dict:
    from benchmark_analyst.reporting import analyze

    if not args.memory_checkpoints:
        raise ValueError("controlled fixture profile requires --memory-checkpoints")
    fixture = LocalFixture.open(args.fixture)
    manifest, config, image = _manifest(
        args, fixture, directory=args.output, schedule=make_schedule(args.requests_per_arm, args.blocks)
    )
    if args.requests_per_arm >= 1000:
        if not args.smoke:
            raise ValueError("main requires matching --smoke with valid mandatory RAM evidence")
        check_smoke(args.smoke, manifest)
    _initialize(args.output, manifest)
    seen_images, settings_guard = set(), {}
    try:
        for slot in manifest["schedule"]:
            _run_slot(
                args,
                fixture,
                args.output,
                manifest,
                config,
                image,
                slot,
                diagnostic_control=False,
                seen_images=seen_images,
                settings_guard=settings_guard,
            )
        _integrity(manifest)
        write_json(args.output / "state.json", {"status": "COMPLETE", "finished_at": utc_now()})
    except BaseException as exc:
        write_json(
            args.output / "state.json",
            {"status": "INCOMPLETE", "finished_at": utc_now(), "error": f"{type(exc).__name__}: {exc}"},
        )
        raise
    result = analyze(args.output)
    print(f"{'VALID' if result['overall_valid'] else 'INVALID'}: {args.output / 'report.html'}", flush=True)
    return result


def diagnose(args) -> dict:
    from benchmark_analyst.reporting import analyze
    from benchmark_analyst.suite_reporting import analyze_suite

    if args.output.exists():
        raise ValueError("suite directory exists; never overwrite or silently resume")
    fixture = LocalFixture.open(args.fixture)
    source = source_identity(ROOT)
    schedule = make_diagnostic_schedule(args.requests_per_arm, args.blocks)
    children, contexts, images_seen = {}, {}, {}
    for variant in DIAGNOSTIC_VARIANTS:
        directory = args.output / variant
        child_schedule = [
            {k: v for k, v in slot.items() if k != "variant"} for slot in schedule if slot["variant"] == variant
        ]
        manifest, config, image = _manifest(
            args,
            fixture,
            directory=directory,
            schedule=child_schedule,
            kind="diagnostic",
            variant=variant,
            source=source,
        )
        manifest["experiment_id"] = f"{args.output.name}-{variant}"
        if args.requests_per_arm >= 1000:
            if not args.smoke:
                raise ValueError("diagnostic campaign requires matching suite --smoke")
            check_smoke(args.smoke / variant, manifest)
        children[variant] = variant
        contexts[variant] = (manifest, config, image)
        images_seen[variant] = set()
    args.output.mkdir(parents=True)
    suite = {
        "schema_version": 1,
        "kind": "diagnostic_suite",
        "experiment_id": args.output.name,
        "mode": "campaign" if args.requests_per_arm >= 1000 else "smoke",
        "status": "RUNNING",
        "requests_per_arm": args.requests_per_arm,
        "blocks": args.blocks,
        "schedule": schedule,
        "children": children,
        "source": source,
        "workload": contexts["D0"][0]["workload"],
        "fixture_sha256": fixture.sha256,
        "suite_events": {"version": 1, "enabled": True},
        "created_at": utc_now(),
    }
    write_json(args.output / "suite.json", suite)
    for variant, (manifest, _, _) in contexts.items():
        _initialize(args.output / variant, manifest)
    settings_guard = {}
    try:
        for sequence, slot in enumerate(schedule, 1):
            variant = slot["variant"]
            manifest, config, image = contexts[variant]
            started = utc_now()
            _run_slot(
                args,
                fixture,
                args.output / variant,
                manifest,
                config,
                image,
                slot,
                diagnostic_control=True,
                seen_images=images_seen[variant],
                settings_guard=settings_guard,
            )
            append_jsonl(
                args.output / "suite_events.jsonl",
                {**slot, "sequence": sequence, "started_at_utc": started, "finished_at_utc": utc_now()},
            )
        for variant, (manifest, _, _) in contexts.items():
            _integrity(manifest)
            write_json(args.output / variant / "state.json", {"status": "COMPLETE", "finished_at": utc_now()})
            analyze(args.output / variant)
        suite["status"], suite["finished_at"] = "COMPLETE", utc_now()
    except BaseException as exc:
        suite.update(status="INCOMPLETE", finished_at=utc_now(), error=f"{type(exc).__name__}: {exc}")
        for variant in DIAGNOSTIC_VARIANTS:
            write_json(args.output / variant / "state.json", {"status": "INCOMPLETE", "error": suite["error"]})
        raise
    finally:
        write_json(args.output / "suite.json", suite)
    result = analyze_suite(args.output)
    print(f"{'VALID' if result['overall_valid'] else 'INVALID'}: {args.output / 'suite_report.html'}", flush=True)
    return result
