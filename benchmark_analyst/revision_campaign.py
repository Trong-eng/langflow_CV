"""Balanced MAIN versus compilation-only campaigns on two pinned local sources."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from benchmark_analyst.benchmark_langflow import verify_workload
from benchmark_analyst.campaign_contract import observed_settings
from benchmark_analyst.fixtures import LocalFixture, verify_fixture_worker
from benchmark_analyst.memory_metrics import CHECKPOINTS, MemoryCollector, resource_declaration, validate_resources
from benchmark_analyst.protocol import (
    append_jsonl,
    file_digest,
    flow_digest,
    measure_overhead,
    source_identity,
    utc_now,
    validate_model_binding,
    write_json,
)
from benchmark_analyst.revision_worker import HARNESS_ROOT, IDENTITY_MODULES, RevisionWorker
from benchmark_analyst.runtime import read_config, run_sample

GROUPS = ("MAIN", "COMPILE")
INTERNAL_ARMS = {"MAIN": "00", "COMPILE": "10"}
RAW_FILES = ("manifest.json", "requests.jsonl", "worker_evidence.jsonl", "memory.jsonl", "state.json")
HARNESS_FILES = (
    "revision_worker.py",
    "revision_campaign.py",
    "revision_reporting.py",
    "runtime.py",
    "worker_observer.py",
    "protocol.py",
    "fixtures.py",
    "memory_metrics.py",
    "campaign_contract.py",
    "benchmark_langflow.py",
    "diagnostic_observer.py",
)
PINNED_MAIN = "c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d"
FIXED_PROFILE = {
    "workers": 1,
    "concurrency": 1,
    "warm_graph": False,
    "warmups": 5,
    "idle_seconds": 5,
    "diagnostics": False,
    "product_telemetry": True,
    "native_tracing": True,
    "access_logging": False,
    "http_client_connection_reuse": True,
}


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def harness_identity() -> dict:
    files = {
        "benchmark_analyst/" + name: file_digest(HARNESS_ROOT / "benchmark_analyst" / name) for name in HARNESS_FILES
    }
    return {"sha256": digest(files), "files": files, "root": str(HARNESS_ROOT)}


def revision_source(root: Path) -> dict:
    source = source_identity(root)
    source["working_tree_clean"] = not subprocess.check_output(["git", "status", "--porcelain"], cwd=root).strip()
    if not source["working_tree_clean"]:
        raise ValueError("application checkout must remain the exact clean committed source")
    paths = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root)
    for raw in paths.split(b"\0"):
        if raw.startswith(b"src/sdk/src/"):
            name = raw.decode()
            if (root / name).is_file():
                source["files"][name] = file_digest(root / name)
    source["sha256"] = digest(source["files"])
    return source


def make_schedule(per_block: int) -> list[dict]:
    if type(per_block) is not int or per_block <= 0:
        raise ValueError("per_block must be a positive integer")
    return [
        {"block": index + 1, "arm": arm, "count": per_block}
        for index, order in enumerate((GROUPS, GROUPS[::-1], GROUPS[::-1], GROUPS))
        for arm in order
    ]


def verify_identity(snapshot: dict, group: str, manifest: dict) -> None:
    identity = snapshot.get("identity", {})
    if Path(identity.get("interpreter", "")).resolve() != Path(manifest["runtime"]["interpreter"]).resolve():
        raise ValueError("worker interpreter differs from the shared pinned environment")
    if identity.get("module_origin_failures"):
        raise ValueError("loaded module imported outside selected checkout")
    declared = manifest["groups"][group]
    root = Path(declared["root"]).resolve()
    if identity.get("group") != group or Path(identity.get("root", "")).resolve() != root:
        raise ValueError("worker declared revision identity differs from selected checkout")
    required = set(IDENTITY_MODULES) | ({"lfx.custom.component_compilation_cache"} if group == "COMPILE" else set())
    if not required.issubset(identity.get("modules", {})):
        raise ValueError("worker critical imported module evidence is missing")
    if identity.get("dependency_sha256") != digest(identity.get("dependency_versions", {})):
        raise ValueError("worker dependency fingerprint differs from declared versions")
    if identity.get("dependency_versions") != snapshot.get("effective", {}).get("versions"):
        raise ValueError("worker dependency fingerprint differs from effective runtime versions")
    for name, module in identity.get("modules", {}).items():
        path = Path(module["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"{name} imported outside {group} checkout")
        relative = path.relative_to(root).as_posix()
        if declared["source"]["files"].get(relative) != module.get("sha256"):
            raise ValueError(f"{name} imported source hash differs from pinned source")
    if not identity.get("modules"):
        raise ValueError("worker imported module evidence is missing")


def check_smoke(directory: Path, manifest: dict) -> None:
    result = json.loads((directory / "validation.json").read_text())
    previous = json.loads((directory / "manifest.json").read_text())
    if result.get("overall_valid") is not True:
        raise ValueError("smoke must have valid latency/output/cache/identity/RAM evidence")
    for name in RAW_FILES:
        path = directory / name
        if not path.is_file() or file_digest(path) != result.get("raw_sha256", {}).get(name):
            raise ValueError(f"smoke raw integrity changed: {name}")
    if validate_run(directory).get("overall_valid") is not True:
        raise ValueError("smoke raw fails recomputed validity")
    for group in GROUPS:
        if previous.get("groups", {}).get(group, {}).get("source") != manifest.get("groups", {}).get(group, {}).get(
            "source"
        ):
            raise ValueError(f"{group} source changed since smoke")
    for key in (
        "kind",
        "source",
        "workload",
        "profile",
        "profile_id",
        "fixture_sha256",
        "warmups",
        "resource_measurement",
    ):
        if previous.get(key) != manifest.get(key):
            raise ValueError(f"{key} changed since smoke")


def verify_snapshot(snapshot: dict, group: str, manifest: dict, slot_data: dict | None = None) -> None:
    verify_identity(snapshot, group, manifest)
    if slot_data:
        verify_fixture_worker(snapshot, slot_data)
    settings, effective = snapshot.get("settings", {}), snapshot.get("effective", {})
    if snapshot.get("configured") != {"compilation_cache": group == "COMPILE", "warm_registry": False}:
        raise ValueError("worker configured treatment differs from selected group")
    if snapshot.get("worker_count") != 1 or settings.get("warm_registry_enabled") is not False:
        raise ValueError("warm graph must be effectively OFF with one worker")
    if snapshot.get("registry_entries") != 0 or snapshot.get("warm", {}).get("hits") != 0:
        raise ValueError("warm registry activity detected while configured OFF")
    if group == "MAIN":
        if (
            snapshot.get("compilation") is not None
            or snapshot.get("compilation_status", {}).get("status") != "unavailable"
        ):
            raise ValueError("MAIN compilation counters must remain unavailable")
        if settings.get("component_compilation_cache_enabled") not in (False, None):
            raise ValueError("MAIN unexpectedly enabled compilation cache")
    elif settings.get("component_compilation_cache_enabled") is not True or not isinstance(
        snapshot.get("compilation"), dict
    ):
        raise ValueError("COMPILE cache is not effectively enabled/measured")
    for key, expected in (
        ("product_telemetry_enabled", True),
        ("native_tracing", True),
        ("uvicorn_access_enabled", False),
        ("gc_enabled", True),
    ):
        if effective.get(key) is not expected:
            raise ValueError(f"effective {key} differs from fixed profile")
    if manifest["profile"].get("effective_common_settings") is not None:
        if observed_settings(snapshot) != manifest["profile"]["effective_common_settings"]:
            raise ValueError("effective dependency/runtime profile differs from smoke or earlier worker")


def check_counters(evidence: dict, count: int) -> dict | None:
    before, warm, after = (evidence[name] for name in CHECKPOINTS[:3])
    for name in ("attempts", "cold", "hits", "errors"):
        values = [snapshot.get("warm", {}).get(name) for snapshot in (before, warm, after)]
        if any(type(v) is not int or v < 0 for v in values) or values != sorted(values):
            raise ValueError("warm counters missing, invalid or reset")
        wanted = count if name in ("attempts", "cold") else 0
        if values[1] - values[0] != (5 if name in ("attempts", "cold") else 0):
            raise ValueError("warmup cold/warm counters differ from five requests")
        if values[2] - values[1] != wanted:
            raise ValueError("measured cold/warm counters differ from request count")
    if evidence["arm"] == "MAIN":
        return None
    snapshots = [snapshot["compilation"] for snapshot in (before, warm, after)]
    keys = ("hits", "misses", "builds", "bypasses", "evictions")
    for key in keys:
        values = [snapshot.get(key) for snapshot in snapshots]
        if any(type(v) is not int or v < 0 for v in values) or values != sorted(values):
            raise ValueError("compilation counters missing, invalid or reset")
    delta = {key: snapshots[2][key] - snapshots[1][key] for key in keys}
    if delta["hits"] <= 0 or any(delta[key] != 0 for key in ("misses", "builds", "bypasses")):
        raise ValueError("COMPILE steady state lacks cache hits or recompiles/bypasses")
    return delta


def _integrity(manifest: dict, fixture: LocalFixture) -> None:
    if harness_identity() != manifest["source"]:
        raise ValueError("shared harness source changed during campaign")
    for group in GROUPS:
        if revision_source(Path(manifest["groups"][group]["root"])) != manifest["groups"][group]["source"]:
            raise ValueError(f"{group} source changed during campaign")
    workload = manifest["workload"]
    for path_key, hash_key in (("image_path", "input_sha256"), ("model_path", "model_sha256")):
        if file_digest(Path(workload[path_key])) != workload[hash_key]:
            raise ValueError("workload bytes changed during campaign")
    if flow_digest(json.loads(Path(workload["flow_export_path"]).read_text())) != workload["flow_sha256"]:
        raise ValueError("frozen flow changed during campaign")
    fixture.verify()


def _slot(
    args, fixture: LocalFixture, manifest: dict, config: dict, image: bytes, slot: dict, seen_outputs: set
) -> None:
    _integrity(manifest, fixture)
    group, block = slot["arm"], slot["block"]
    data = fixture.clone(args.output / "slots" / f"b{block:02d}_{group}")
    worker = RevisionWorker(
        Path(manifest["groups"][group]["root"]),
        args.port,
        group=group,
        env_file=args.env_file,
        credentials_file=args.credentials_file,
        fixture=data,
        controlled=True,
    )
    evidence, success = {**slot, "fixture_sha256": fixture.sha256, "slot_fixture": data}, False
    print(f"{args.output.name} block={block} group={group}: fresh worker", flush=True)
    try:
        worker.start(group, args.output / "logs" / f"b{block:02d}_{group}")
        with worker.session() as session:
            startup = worker.snapshot(session)
            verify_snapshot(startup, group, manifest, data)
            observed = observed_settings(startup)
            if manifest["profile"].get("effective_common_settings") is None:
                manifest["profile"]["effective_common_settings"] = observed
                manifest["profile_id"] = digest(manifest["profile"])
                write_json(args.output / "manifest.json", manifest)
            verify_workload(worker, session, config, expected_digest=manifest["workload"]["flow_sha256"])
            before = worker.snapshot(session)
            verify_snapshot(before, group, manifest, data)
            evidence["before_warmup"] = before
            collector = MemoryCollector(manifest["experiment_id"], block, INTERNAL_ARMS[group], before)
            collector.arm = group  # Collector identity is generic after its legacy constructor check.

            def memory(checkpoint, snapshot, last=None):
                row = collector.collect(checkpoint, snapshot=snapshot, last_sample_finished=last)
                append_jsonl(args.output / "memory.jsonl", row)
                if row["rss"]["status"] != "measured":
                    raise ValueError("mandatory whole-worker RSS unavailable")

            memory("before_warmup", before)
            last = None
            session_id = f"revision-{manifest['experiment_id']}-{block}-{group}-{uuid4()}"
            for phase, count in (("warmup", 5), ("measured", slot["count"])):
                for index in range(count):
                    row = run_sample(
                        session,
                        worker.base_url,
                        config,
                        image,
                        Path(config["image_path"]).name,
                        worker.token,
                        block=block,
                        arm=group,
                        phase=phase,
                        index=index,
                        session_id=session_id,
                    )
                    last = time.monotonic()
                    path = row.get("output_image_path")
                    if path and path in seen_outputs:
                        row.update(outcome="invalid", output_valid=False, error="output URL reused")
                    if path:
                        seen_outputs.add(path)
                    append_jsonl(args.output / "requests.jsonl", row)
                    if row.get("outcome") != "success" or not row.get("output_valid") or row.get("warm_path") != "cold":
                        raise ValueError(f"invalid {phase} attempt {index}: {row.get('error') or row.get('warm_path')}")
                    if phase == "measured" and (index + 1) % 50 == 0:
                        print(f"  measured {index + 1}/{count}", flush=True)
                checkpoint = "after_warmup" if phase == "warmup" else "after_measurement"
                snapshot = worker.snapshot(session)
                evidence[checkpoint] = snapshot
                verify_snapshot(snapshot, group, manifest, data)
                memory(checkpoint, snapshot, last)
                if (
                    phase == "warmup"
                    and group == "COMPILE"
                    and snapshot["compilation"]["hits"] <= before["compilation"]["hits"]
                ):
                    raise ValueError("COMPILE warmup did not demonstrate cache hits")
            evidence["compilation_delta"] = check_counters(evidence, slot["count"])
            remaining = last + 5 - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            idle = worker.snapshot(session, fresh_connection=True)
            evidence["after_idle"] = idle
            verify_snapshot(idle, group, manifest, data)
            memory("after_idle", idle, last)
            with worker.session() as final_session:
                verify_workload(worker, final_session, config, expected_digest=manifest["workload"]["flow_sha256"])
            _integrity(manifest, fixture)
            success = True
    finally:
        append_jsonl(args.output / "worker_evidence.jsonl", evidence)
        worker.stop()
        append_jsonl(args.output / "worker_cleanup.jsonl", {"block": block, "arm": group, "stopped": True})
        if success:
            fixture.release(data)
            append_jsonl(
                args.output / "fixture_cleanup.jsonl", {"block": block, "arm": group, "removed_owned_slot": True}
            )


def run(args) -> dict:
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("output destination exists; never overwrite an attempt")
    if args.per_block not in (2, 250):
        raise ValueError("fixed design permits per-block 2 smoke or 250 campaign")
    fixture = LocalFixture.open(args.fixture)
    config = read_config(args.config)
    if not config.get("reference_pixel_sha256"):
        raise ValueError("frozen output reference is required")
    flow = json.loads(Path(config["flow_export_path"]).read_text())
    validate_model_binding(flow, config)
    workload = {
        **config,
        "flow_sha256": flow_digest(flow),
        "input_sha256": file_digest(Path(config["image_path"])),
        "model_sha256": file_digest(Path(config["model_path"])),
    }
    profile = {**FIXED_PROFILE, "effective_common_settings": None}
    if args.smoke:
        profile = json.loads((args.smoke / "manifest.json").read_text())["profile"]
    manifest = {
        "schema_version": 2,
        "kind": "revision_comparison",
        "experiment_id": args.output.name,
        "mode": "campaign" if args.per_block == 250 else "smoke",
        "created_at": utc_now(),
        "groups": {
            group: {"root": str(root.resolve()), "source": revision_source(root.resolve())}
            for group, root in zip(GROUPS, (args.main_root, args.compile_root), strict=True)
        },
        "source": harness_identity(),
        "workload": workload,
        "fixture_sha256": fixture.sha256,
        "profile": profile,
        "profile_id": digest(profile),
        "warmups": 5,
        "blocks": 4,
        "per_block": args.per_block,
        "requests_per_arm": 4 * args.per_block,
        "schedule": make_schedule(args.per_block),
        "resource_measurement": resource_declaration(),
        "runtime": {"interpreter": sys.executable, "workers": 1, "concurrency": 1},
        "primary_metrics": ["server_total_ms", "flow_api_ms"],
        "measurement_boundary": "Server: outer production ASGI entry through final response body send; client: requests.Session.post /run through complete response bytes. Upload, download/validation and all checkpoint probes are outside these clocks.",
        "smoke_directory": str(args.smoke) if args.smoke else None,
    }
    if manifest["groups"]["MAIN"]["source"]["git_revision"] != PINNED_MAIN:
        raise ValueError("MAIN is not the pinned LOCAL main commit")
    if args.per_block == 250:
        if not args.smoke:
            raise ValueError("full campaign requires matching valid smoke for both sources")
        check_smoke(args.smoke, manifest)
    args.output.mkdir(parents=True)
    write_json(args.output / "manifest.json", manifest)
    for name in RAW_FILES[1:4]:
        (args.output / name).touch()
    write_json(args.output / "state.json", {"status": "RUNNING", "started_at": utc_now()})
    try:
        outputs = set()
        for slot in manifest["schedule"]:
            _slot(args, fixture, manifest, config, Path(config["image_path"]).read_bytes(), slot, outputs)
        _integrity(manifest, fixture)
        write_json(args.output / "state.json", {"status": "COMPLETE", "finished_at": utc_now()})
    except BaseException as exc:
        write_json(
            args.output / "state.json",
            {"status": "INCOMPLETE", "finished_at": utc_now(), "error": f"{type(exc).__name__}: {exc}"},
        )
        raise
    result = validate_run(args.output)
    write_json(args.output / "validation.json", result)
    if not result["overall_valid"]:
        raise ValueError("completed campaign failed validity: " + "; ".join(result["errors"][:8]))
    print(f"VALID {manifest['mode']}: {args.output}", flush=True)
    return result


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC and timezone aware")
    return parsed


def verify_probe_boundaries(selected: list[dict], item: dict, memory: list[dict]) -> None:
    previous_end = None
    for row in selected:
        start, end = timestamp(row["started_at"]), timestamp(row["finished_at"])
        if end < start or previous_end and start < previous_end:
            raise ValueError("request intervals overlap or run backwards")
        previous_end = end
    warmup = [r for r in selected if r["phase"] == "warmup"]
    measured = [r for r in selected if r["phase"] == "measured"]
    for checkpoint in CHECKPOINTS:
        snapshot = item[checkpoint]
        probe_start = timestamp(snapshot["snapshot_started_at_utc"])
        duration = snapshot["snapshot_duration_ms"]
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            raise ValueError("snapshot duration invalid")
        probe_end = probe_start + timedelta(milliseconds=duration)
        records = [
            r
            for r in memory
            if r.get("block") == item["block"] and r.get("arm") == item["arm"] and r.get("checkpoint") == checkpoint
        ]
        if len(records) != 1:
            raise ValueError("memory checkpoint is missing or duplicated")
        memory_start, memory_end = timestamp(records[0]["started_at_utc"]), timestamp(records[0]["finished_at_utc"])
        if probe_end > memory_start:
            raise ValueError("identity probe overlaps RAM checkpoint")
        if checkpoint == "before_warmup":
            if memory_end > timestamp(warmup[0]["started_at"]):
                raise ValueError("pre-warmup probe overlaps workload")
        else:
            last_request = warmup[-1] if checkpoint == "after_warmup" else measured[-1]
            if probe_start < timestamp(last_request["finished_at"]):
                raise ValueError("checkpoint probe overlaps workload")
            if checkpoint == "after_warmup" and memory_end > timestamp(measured[0]["started_at"]):
                raise ValueError("post-warmup checkpoint overlaps measurement")
            if checkpoint == "after_idle" and probe_start < timestamp(last_request["finished_at"]) + timedelta(
                seconds=5
            ):
                raise ValueError("idle checkpoint starts before five seconds")


def validate_run(directory: Path) -> dict:
    """Strict offline validation; raw is never edited and invalid samples stay visible."""
    directory = Path(directory)
    errors, counts, compilation_evidence = [], {}, []
    hashes = {name: file_digest(directory / name) for name in RAW_FILES if (directory / name).is_file()}
    result = {
        "overall_valid": False,
        "valid": False,
        "errors": errors,
        "raw_sha256": hashes,
        "counts": counts,
        "compilation_evidence": compilation_evidence,
    }
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        state = json.loads((directory / "state.json").read_text())
        rows, evidence, memory = (_jsonl(directory / name) for name in RAW_FILES[1:4])
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"raw could not be read: {type(exc).__name__}")
        return result
    try:
        if state.get("status") != "COMPLETE":
            errors.append("campaign state is not COMPLETE")
        if manifest.get("schema_version") != 2 or manifest.get("kind") != "revision_comparison":
            errors.append("manifest is not the revision comparison contract")
        if manifest.get("per_block") not in (2, 250) or type(manifest.get("per_block")) is not int:
            errors.append("per_block differs from fixed smoke/campaign design")
        if manifest.get("requests_per_arm") != 4 * manifest["per_block"] or manifest.get("mode") != (
            "campaign" if manifest["per_block"] == 250 else "smoke"
        ):
            errors.append("mode/request count differs from fixed design")
        if (
            set(manifest.get("groups", {})) != set(GROUPS)
            or manifest["groups"]["MAIN"]["source"]["git_revision"] != PINNED_MAIN
        ):
            errors.append("source groups or pinned LOCAL main revision differ")
        for source in [manifest["source"], *(manifest["groups"][group]["source"] for group in GROUPS)]:
            if not source.get("files") or digest(source["files"]) != source.get("sha256"):
                errors.append("source file fingerprint differs from declared hash")
        if any(manifest["groups"][group]["source"].get("working_tree_clean") is not True for group in GROUPS):
            errors.append("application source is not an exact clean committed checkout")
        if any(
            type(manifest["profile"].get(key)) is not type(value) or manifest["profile"].get(key) != value
            for key, value in FIXED_PROFILE.items()
        ):
            errors.append("fixed runtime profile settings changed")
        if manifest.get("runtime", {}).get("workers") != 1 or manifest.get("runtime", {}).get("concurrency") != 1:
            errors.append("runtime worker/concurrency declaration differs from fixed profile")
        if (
            manifest.get("schedule") != make_schedule(manifest["per_block"])
            or manifest.get("warmups") != 5
            or manifest.get("blocks") != 4
        ):
            errors.append("fixed schedule/warmup/block contract changed")
        if manifest.get("profile_id") != digest(manifest["profile"]) or not manifest["profile"].get(
            "effective_common_settings"
        ):
            errors.append("effective runtime profile is missing or hash differs")
        grouped = defaultdict(list)
        request_ids, outputs = set(), set()
        for row in rows:
            slot = row.get("block"), row.get("arm")
            grouped[slot].append(row)
            request_id = row.get("request_id")
            if not request_id or request_id in request_ids:
                errors.append("request identity missing or reused")
            request_ids.add(request_id)
            if row.get("outcome") != "success" or row.get("output_valid") is not True:
                errors.append(f"failed attempt retained: {slot}/{row.get('phase')}/{row.get('index')}")
            if row.get("image_pixel_sha256") != manifest["workload"]["reference_pixel_sha256"]:
                errors.append("output pixel hash differs from reference")
            path = row.get("output_image_path")
            if not path or path in outputs:
                errors.append("output image URL missing or reused")
            outputs.add(path)
            if row.get("warm_path") != "cold":
                errors.append("request used warm graph or lacks path evidence")
            try:
                measured = measure_overhead(
                    {**row, "complete": row.get("outcome") == "success", "status_code": row.get("http_status")},
                    manifest["workload"]["scrfd_node_ids"],
                )
                if any(not math.isclose(row[key], value, abs_tol=1e-7) for key, value in measured.items()):
                    errors.append("stored timing does not match component interval union")
                if (
                    any(
                        type(row.get(key)) not in (int, float) or not math.isfinite(row[key]) or row[key] < 0
                        for key in ("flow_api_ms", "upload_ms")
                    )
                    or row.get("flow_api_ms", 0) <= 0
                ):
                    errors.append("client timing is invalid")
            except (ValueError, TypeError, KeyError):
                errors.append("invalid server/component timing")
        expected_order = [(s["block"], s["arm"]) for s in manifest["schedule"]]
        if [(e.get("block"), e.get("arm")) for e in evidence] != expected_order:
            errors.append("worker evidence coverage/order differs from schedule")
        if set(grouped) != set(expected_order):
            errors.append("request slots differ from schedule")
        for slot in manifest["schedule"]:
            selected = grouped[(slot["block"], slot["arm"])]
            expected = [("warmup", i) for i in range(5)] + [("measured", i) for i in range(slot["count"])]
            if [(r.get("phase"), r.get("index")) for r in selected] != expected:
                errors.append(f"wrong request count/order in {slot['block']}/{slot['arm']}")
        for group in GROUPS:
            counts[group] = dict(Counter(r.get("phase") for r in rows if r.get("arm") == group))
        for item in evidence:
            group = item["arm"]
            if (
                item.get("fixture_sha256") != manifest["fixture_sha256"]
                or item.get("slot_fixture", {}).get("fixture_sha256") != manifest["fixture_sha256"]
            ):
                errors.append("slot fixture hash differs from frozen baseline")
            for checkpoint in CHECKPOINTS:
                verify_snapshot(item[checkpoint], group, manifest, item["slot_fixture"])
            pids = {item[name]["pid"] for name in CHECKPOINTS}
            if len(pids) != 1 or any(r.get("pid") not in pids for r in grouped[(item["block"], group)]):
                errors.append("request and checkpoint worker PIDs differ")
            if item["after_idle"].get("compilation") != item["after_measurement"].get("compilation") or item[
                "after_idle"
            ].get("warm") != item["after_measurement"].get("warm"):
                errors.append("cache/warm counters changed during idle interval")
            verify_probe_boundaries(grouped[(item["block"], group)], item, memory)
            delta = check_counters(item, item["count"])
            if item.get("compilation_delta") != delta:
                errors.append("recorded compilation delta differs from checkpoints")
            compilation_evidence.append(
                {
                    "block": item["block"],
                    "arm": group,
                    "delta": delta,
                    "status": "unavailable" if delta is None else "measured",
                }
            )

        def mapped(items):
            mapped_items = copy.deepcopy(items)
            for item in mapped_items:
                item["arm"] = INTERNAL_ARMS.get(item.get("arm"), item.get("arm"))
            return mapped_items

        resource_manifest = {**manifest, "schedule": mapped(manifest["schedule"])}
        resources = validate_resources(resource_manifest, mapped(memory), mapped(evidence))
        errors.extend(resources["errors"])
        result["resource_valid"] = resources["valid"]
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        errors.append(f"contract validation failed: {type(exc).__name__}: {exc}")
    result.update(overall_valid=not errors, valid=not errors)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("main-root", "compile-root", "config", "fixture", "credentials-file", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--env-file", type=Path, default=HARNESS_ROOT / ".env")
    parser.add_argument("--smoke", type=Path)
    parser.add_argument("--port", type=int, default=7876)
    parser.add_argument("--per-block", type=int, default=2)
    run(parser.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
