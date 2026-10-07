"""Offline validation and reports for the SCRFD Langflow overhead experiment.

This module neither starts a server nor performs requests. Raw files are read-only;
only derived report artifacts in the requested run directory are replaced.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

ARMS = ("00", "01", "10", "11")
PAIRED_ARMS = ("10", "11")
BLOCK_COUNT = 4
SCRFD_NODE_COUNT = 4
SLOT_COUNT = BLOCK_COUNT * len(ARMS)
SHA256_LENGTH = 64
CAMPAIGN_REQUESTS_PER_ARM = 1000
PRIMARY = "langflow_overhead_ms"
METRICS = (PRIMARY, "server_total_ms", "scrfd_processing_ms", "flow_api_ms", "upload_ms")
SNAPSHOTS = ("before_warmup", "after_warmup", "after_measurement")
BOUNDARY = (
    "langflow_overhead_ms = server_total_ms - scrfd_processing_ms. "
    "server_total_ms là wall-clock trong worker từ điểm vào middleware benchmark "
    "của /run đến khi gửi xong response body. scrfd_processing_ms là độ dài hợp "
    "các khoảng output method của bốn node SCRFD, không cộng hai lần phần chồng lấn. "
    "Upload, tải ảnh kết quả và toàn bộ xử lý SCRFD nằm ngoài chỉ số chính. "
    "Chat Input/Output, tạo graph/component, validation, auth bên trong middleware "
    "và serialize response vẫn được tính. Đây không phải CPU time hay latency end-user."
)


def _finite(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _integer(value: Any, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _read_json(path: Path, errors: list[str]) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"{path.name}: {type(exc).__name__}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path.name}: expected a JSON object")
        return {}
    return value


def _read_jsonl(path: Path, errors: list[str]) -> list[dict]:
    result = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    errors.append(f"{path.name}:{line_number}: invalid JSON object")
                    continue
                if isinstance(value, dict):
                    result.append(value)
                else:
                    errors.append(f"{path.name}:{line_number}: expected JSON object")
    except OSError as exc:
        errors.append(f"{path.name}: {type(exc).__name__}")
    return result


def _percentile(values: list[float], fraction: float) -> float:
    """Linear interpolation between order statistics (Hyndman-Fan type 7)."""
    position = (len(values) - 1) * fraction
    left = math.floor(position)
    right = math.ceil(position)
    return values[left] + (values[right] - values[left]) * (position - left)


def _stats(rows: list[dict], metric: str) -> dict:
    values = sorted(float(row[metric]) for row in rows if _finite(row.get(metric)))
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None}
    return {
        "n": len(values),
        "mean": fmean(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
    }


def _summarize(rows: list[dict]) -> dict:
    successes = [row for row in rows if row.get("outcome") == "success" and row.get("output_valid") is True]
    outcomes = Counter(str(row.get("outcome", "missing")) for row in rows)
    return {
        "attempted": len(rows),
        "successful": len(successes),
        "outcomes": dict(outcomes),
        **{metric: _stats(successes, metric) for metric in METRICS},
    }


def _slot(row: dict) -> tuple[int, str] | None:
    block, arm = row.get("block"), row.get("arm")
    if not _integer(block, 1) or not isinstance(arm, str) or arm not in ARMS:
        return None
    return block, arm


def _arms(manifest: dict) -> tuple[str, ...]:
    return PAIRED_ARMS if manifest.get("kind", "factorial") == "diagnostic" else ARMS


def _validate_manifest(manifest: dict, errors: list[str]) -> dict[tuple[int, str], int]:
    if manifest.get("schema_version") != 1:
        errors.append("manifest: unsupported or missing schema_version")
    experiment_id = manifest.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        errors.append("manifest: missing or invalid experiment_id")
    kind = manifest.get("kind", "factorial")
    if kind not in ("factorial", "diagnostic"):
        errors.append("manifest: unsupported kind")
    arms = _arms(manifest)
    if manifest.get("primary_metric") != PRIMARY:
        errors.append("manifest: wrong primary_metric")
    if manifest.get("blocks") != BLOCK_COUNT:
        errors.append("manifest: requires four balanced blocks")
    count = manifest.get("requests_per_arm")
    if not _integer(count, 4) or count % 4:
        errors.append("manifest: requests_per_arm must be a positive multiple of four")
        count = 0
    if not _integer(manifest.get("warmups"), 1):
        errors.append("manifest: warmups must be a positive integer")
    workload = _object(manifest.get("workload"))
    nodes = workload.get("scrfd_node_ids")
    if (
        not isinstance(nodes, list)
        or len(nodes) != SCRFD_NODE_COUNT
        or not all(isinstance(node, str) and node for node in nodes)
    ):
        errors.append("manifest: workload must identify four SCRFD nodes")
    elif len(set(nodes)) != SCRFD_NODE_COUNT:
        errors.append("manifest: SCRFD node identifiers must be distinct")
    for key in ("reference_pixel_sha256", "flow_sha256", "input_sha256", "model_sha256"):
        digest = workload.get(key)
        if (
            not isinstance(digest, str)
            or len(digest) != SHA256_LENGTH
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            errors.append(f"manifest: workload.{key} must be a SHA256 digest")
    source_digest = _object(manifest.get("source")).get("sha256")
    if (
        not isinstance(source_digest, str)
        or len(source_digest) != SHA256_LENGTH
        or any(char not in "0123456789abcdef" for char in source_digest)
    ):
        errors.append("manifest: frozen source provenance requires a SHA256 digest")
    if manifest.get("integrity_valid") is False:
        errors.append("manifest: runner detected workload/source integrity drift")
    schedule = manifest.get("schedule")
    if not isinstance(schedule, list):
        errors.append("manifest: missing schedule")
        return {}
    expected = {}
    orders = defaultdict(list)
    for item in schedule:
        if not isinstance(item, dict) or _slot(item) is None:
            errors.append("manifest: invalid scheduled slot")
            continue
        slot = _slot(item)
        if slot in expected:
            errors.append(f"manifest: duplicate slot {slot}")
        if item.get("count") != count // 4 or not _integer(item.get("count"), 1):
            errors.append(f"manifest: wrong request count in slot {slot}")
        expected[slot] = item.get("count", 0) if _integer(item.get("count")) else 0
        orders[slot[0]].append(slot[1])
    if set(expected) != {(block, arm) for block in range(1, 5) for arm in arms}:
        errors.append("manifest: schedule must contain every arm in every block exactly once")
    if len(schedule) != BLOCK_COUNT * len(arms) or any(len(orders[block]) != len(arms) for block in range(1, 5)):
        errors.append(f"manifest: schedule requires {BLOCK_COUNT * len(arms)} slots")
    elif kind == "diagnostic":
        balanced = [("10", "11"), ("11", "10"), ("11", "10"), ("10", "11")]
        if [tuple(orders[block]) for block in range(1, 5)] != balanced:
            errors.append("manifest: diagnostic pair order is not balanced")
    else:
        for position in range(4):
            if {orders[block][position] for block in range(1, 5)} != set(ARMS):
                errors.append("manifest: arm order is not balanced across positions")
                break
    return expected


def _validate_timing(row: dict, expected_nodes: set[str], prefix: str, errors: list[str]) -> None:
    errors.extend(f"{prefix}: invalid {metric}" for metric in METRICS if not _finite(row.get(metric)))
    total, processing, overhead = (row.get(key) for key in ("server_total_ms", "scrfd_processing_ms", PRIMARY))
    if all(_finite(value) for value in (total, processing, overhead)) and (
        processing > total or not math.isclose(total - processing, overhead, abs_tol=0.01, rel_tol=1e-7)
    ):
        errors.append(f"{prefix}: overhead subtraction does not match server/SCRFD timing")
    spans = row.get("component_intervals_ms")
    if not isinstance(spans, list) or not spans:
        errors.append(f"{prefix}: missing component intervals")
        return
    intervals, nodes = [], set()
    for span in spans:
        if not isinstance(span, dict):
            errors.append(f"{prefix}: malformed component interval")
            continue
        node, start, end = span.get("node_id"), span.get("start_ms"), span.get("end_ms")
        if isinstance(node, str) and node in expected_nodes:
            nodes.add(node)
        if not _finite(start) or not _finite(end) or end < start or (_finite(total) and end > total + 0.01):
            errors.append(f"{prefix}: component interval outside server timing boundary")
            continue
        if isinstance(node, str) and node in expected_nodes:
            intervals.append((start, end))
    if nodes != expected_nodes or len(expected_nodes) != SCRFD_NODE_COUNT:
        errors.append(f"{prefix}: component intervals must cover all four SCRFD nodes")
    union, right = 0.0, 0.0
    for start, end in sorted(intervals):
        union += max(0.0, end - max(start, right))
        right = max(right, end)
    if _finite(processing) and not math.isclose(union, processing, abs_tol=0.01, rel_tol=1e-7):
        errors.append(f"{prefix}: SCRFD timing is not the interval union")


def _validate_rows(manifest: dict, rows: list[dict], expected: dict, errors: list[str]) -> dict:
    grouped = defaultdict(list)
    request_ids = set()
    observed_order = []
    workload = _object(manifest.get("workload"))
    node_list = workload.get("scrfd_node_ids", [])
    expected_nodes = {node for node in node_list if isinstance(node, str)} if isinstance(node_list, list) else set()
    for number, row in enumerate(rows, start=1):
        prefix = f"request row {number}"
        slot = _slot(row)
        if slot not in expected:
            errors.append(f"{prefix}: unknown slot")
        else:
            grouped[slot].append(row)
            if not observed_order or observed_order[-1] != slot:
                observed_order.append(slot)
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in request_ids:
            errors.append(f"{prefix}: missing/duplicate request_id")
        else:
            request_ids.add(request_id)
        if row.get("phase") not in ("warmup", "measured"):
            errors.append(f"{prefix}: unexpected phase")
        if row.get("outcome") != "success" or row.get("output_valid") is not True or row.get("error"):
            errors.append(f"{prefix}: request failed or output invalid")
        if row.get("image_pixel_sha256") != workload.get("reference_pixel_sha256") or not row.get("image_pixel_sha256"):
            errors.append(f"{prefix}: output pixels do not match reference")
        if not _integer(row.get("pid"), 1):
            errors.append(f"{prefix}: missing/invalid worker PID")
        if slot and row.get("phase") == "measured":
            wanted = "warm" if slot[1][1] == "1" else "cold"
            if row.get("warm_path") != wanted:
                errors.append(f"{prefix}: measured warm path is not {wanted}")
        _validate_timing(row, expected_nodes, prefix, errors)
    if observed_order != list(expected):
        errors.append("requests: actual slot order does not match scheduled order")
    for slot, count in expected.items():
        slot_rows = grouped[slot]
        for phase, wanted in (("warmup", manifest.get("warmups")), ("measured", count)):
            phase_rows = [row for row in slot_rows if row.get("phase") == phase]
            if len(phase_rows) != wanted:
                errors.append(f"slot {slot}: {phase} attempts {len(phase_rows)} != {wanted}")
            indices = [row.get("index") for row in phase_rows]
            if any(not _integer(index) for index in indices) or len({str(index) for index in indices}) != len(indices):
                errors.append(f"slot {slot}: missing/duplicate {phase} indices")
        phases = [row.get("phase") for row in slot_rows]
        if (
            "warmup" in phases
            and "measured" in phases
            and phases.index("measured") < len(phases) - phases[::-1].index("warmup")
        ):
            errors.append(f"slot {slot}: warmup must precede measurement")
    return grouped


def _validate_evidence(evidence: list[dict], expected: dict, grouped: dict, errors: list[str]) -> list[dict]:
    by_slot = defaultdict(list)
    for item in evidence:
        slot = _slot(item)
        if slot not in expected:
            errors.append("worker evidence: unknown slot")
        else:
            by_slot[slot].append(item)
    diagnostics = []
    for slot in expected:
        label = f"worker slot {slot}"
        if len(by_slot[slot]) != 1:
            errors.append(f"{label}: expected exactly one evidence record")
            continue
        item = by_slot[slot][0]
        snapshots = [_object(item.get(name)) for name in SNAPSHOTS]
        pids = [snapshot.get("pid") for snapshot in snapshots] + [row.get("pid") for row in grouped[slot]]
        if not pids or any(not _integer(pid, 1) for pid in pids) or len({str(pid) for pid in pids}) != 1:
            errors.append(f"{label}: worker PID changed or missing")
        for phase, snapshot in zip(SNAPSHOTS, snapshots, strict=True):
            settings = _object(snapshot.get("settings"))
            wanted = {
                "component_compilation_cache_enabled": slot[1][0] == "1",
                "warm_registry_enabled": slot[1][1] == "1",
                "http_connection_reuse_enabled": False,
            }
            for key, value in wanted.items():
                if settings.get(key) is not value:
                    errors.append(f"{label}/{phase}: wrong or missing {key}")
            if not _integer(snapshot.get("registry_entries")):
                errors.append(f"{label}/{phase}: missing registry_entries")
        deltas = {}
        for group, keys in (("compilation", ("hits", "bypasses")), ("warm", ("hits", "cold"))):
            for key in keys:
                values = [_object(snapshot.get(group)).get(key) for snapshot in snapshots]
                if any(not _integer(value) for value in values):
                    errors.append(f"{label}: missing/noninteger {group}.{key} counters")
                    continue
                if values[1] < values[0] or values[2] < values[1]:
                    errors.append(f"{label}: reset {group}.{key} counters")
                deltas[f"{group}_{key}"] = values[2] - values[1]
        for group in ("compilation", "warm"):
            for key in ("misses", "builds", "evictions"):
                values = [_object(snapshot.get(group)).get(key) for snapshot in snapshots]
                if all(_integer(value) for value in values):
                    if values[1] < values[0] or values[2] < values[1]:
                        errors.append(f"{label}: reset {group}.{key} counters")
                    deltas[f"{group}_{key}"] = values[2] - values[1]
        attempts = len([row for row in grouped[slot] if row.get("phase") == "measured"])
        warm_on, compilation_on = slot[1][1] == "1", slot[1][0] == "1"
        if deltas.get("warm_hits") != (attempts if warm_on else 0):
            errors.append(f"{label}: measured warm hit count does not match arm/attempts")
        if deltas.get("warm_cold") != (0 if warm_on else attempts):
            errors.append(f"{label}: measured cold count does not match arm/attempts")
        hits, bypasses = deltas.get("compilation_hits"), deltas.get("compilation_bypasses")
        if compilation_on and (hits is None or hits <= 0):
            errors.append(f"{label}: compilation ON lacks measured hits")
        if not compilation_on and (hits != 0 or bypasses is None or bypasses <= 0):
            errors.append(f"{label}: compilation OFF requires zero hits and measured bypasses")
        events = deltas.get("compilation_hits", 0) + deltas.get("compilation_misses", 0)
        diagnostics.append(
            {
                "block": slot[0],
                "arm": slot[1],
                "pid": snapshots[-1].get("pid"),
                "measured_attempts": attempts,
                **deltas,
                "compilation_hit_rate": deltas.get("compilation_hits", 0) / events
                if events and "compilation_misses" in deltas
                else None,
                "effective": _object(snapshots[-1].get("effective")),
                "cache_occupancy": {
                    phase: {
                        key: snapshot[key]
                        for key in (
                            "registry_entries",
                            "cache_accounting",
                            "accounting",
                            "compilation_accounting",
                            "warm_accounting",
                        )
                        if key in snapshot
                    }
                    for phase, snapshot in zip(SNAPSHOTS, snapshots, strict=True)
                },
            }
        )
    return diagnostics


def _decomposition(manifest: dict, rows: list[dict]) -> dict:
    """Six sequential method bodies partition the response envelope, not CPU work."""
    node_ids = _object(manifest.get("workload")).get("scrfd_node_ids", [])
    scrfd = {node for node in node_ids if isinstance(node, str)} if isinstance(node_ids, list) else set()
    records, unavailable = [], []
    metrics = ("pre_ms", "gaps_ms", "post_ms", "chat_ms")
    for row in rows:
        identity = {key: row.get(key) for key in ("block", "arm", "index", "request_id")}
        spans = row.get("component_intervals_ms")
        reason = None
        if not isinstance(spans, list) or len(spans) != 6 or any(not isinstance(span, dict) for span in spans):
            reason = "requires six method spans including Chat Input and Chat Output"
        elif any(not _finite(span.get(key)) for span in spans for key in ("start_ms", "end_ms")) or any(
            not isinstance(span.get("node_id"), str) for span in spans
        ):
            reason = "invalid method span"
        else:
            spans = sorted(spans, key=lambda span: span.get("start_ms", -1))
            nodes = [span.get("node_id") for span in spans]
            chat = [
                node
                for node in nodes
                if isinstance(node, str) and node.lower().replace("-", "").startswith(("chatinput", "chatoutput"))
            ]
            if (
                len(set(nodes)) != 6
                or set(nodes) - set(chat) != scrfd
                or len(chat) != 2
                or not any("input" in node.lower() for node in chat)
                or not any("output" in node.lower() for node in chat)
            ):
                reason = "missing or ambiguous Chat Input/Output method spans"
            elif any(not _finite(span.get(key)) for span in spans for key in ("start_ms", "end_ms")) or any(
                span["end_ms"] < span["start_ms"] for span in spans
            ):
                reason = "invalid method span"
            elif any(left["end_ms"] > right["start_ms"] for left, right in zip(spans, spans[1:], strict=False)):
                reason = "overlapping method spans; sequential decomposition unavailable"
            elif not _finite(row.get("server_total_ms")) or spans[-1]["end_ms"] > row["server_total_ms"]:
                reason = "method spans outside response envelope"
            else:
                segments = {
                    "pre_ms": spans[0]["start_ms"],
                    "gaps_ms": sum(
                        right["start_ms"] - left["end_ms"] for left, right in zip(spans, spans[1:], strict=False)
                    ),
                    "post_ms": row["server_total_ms"] - spans[-1]["end_ms"],
                    "chat_ms": sum(span["end_ms"] - span["start_ms"] for span in spans if span["node_id"] in chat),
                }
                if not _finite(row.get(PRIMARY)) or not math.isclose(
                    sum(segments.values()), row[PRIMARY], abs_tol=0.01, rel_tol=1e-7
                ):
                    reason = "decomposition checksum does not match overhead"
                else:
                    records.append({**identity, **segments, "overhead_ms": row[PRIMARY]})
        if reason:
            unavailable.append({**identity, "reason": reason})

    def summarize(selected: list[dict]) -> dict:
        return {metric: _stats(selected, metric) for metric in metrics}

    return {
        "status": "measured" if records and not unavailable else "partial" if records else "unavailable",
        "records": records,
        "unavailable": unavailable,
        "arms": {arm: summarize([row for row in records if row["arm"] == arm]) for arm in _arms(manifest)},
        "blocks": {
            str(block): {
                arm: summarize([row for row in records if row["block"] == block and row["arm"] == arm])
                for arm in _arms(manifest)
            }
            for block in range(1, 5)
        },
        "note": "Pre-method time includes middleware/auth/flow/setup/dispatch; it is not graph-copy time.",
    }


def _tail_diagnostics(rows: list[dict], blocks: dict, decomposition: dict) -> dict:
    keys = ("mean", "p50", "p95", "p99")
    paired = {}
    windows = {}
    for block, summaries in blocks.items():
        paired[block] = {
            key: summaries["11"][PRIMARY][key] - summaries["10"][PRIMARY][key]
            if summaries["11"][PRIMARY][key] is not None and summaries["10"][PRIMARY][key] is not None
            else None
            for key in keys
        }
        paired[block]["segments"] = {
            metric: decomposition["blocks"][block]["11"][metric]["mean"]
            - decomposition["blocks"][block]["10"][metric]["mean"]
            if decomposition["blocks"][block]["11"][metric]["mean"] is not None
            and decomposition["blocks"][block]["10"][metric]["mean"] is not None
            else None
            for metric in ("pre_ms", "gaps_ms", "post_ms", "chat_ms")
        }
        windows[block] = {}
        for arm in summaries:
            samples = sorted(
                [row for row in rows if row.get("block") == int(block) and row.get("arm") == arm],
                key=lambda row: row["index"] if _integer(row.get("index")) else -1,
            )
            windows[block][arm] = {
                "early": _stats(samples[:50], PRIMARY),
                "late": _stats(samples[-50:], PRIMARY),
                "overlap": len(samples) < 100,
            }
    sensitivity = {}
    for block in range(1, 5):
        means = {
            arm: _stats([row for row in rows if row.get("block") != block and row.get("arm") == arm], PRIMARY)["mean"]
            for arm in PAIRED_ARMS
        }
        sensitivity[str(block)] = (
            means["11"] - means["10"] if all(value is not None for value in means.values()) else None
        )
    return {
        "paired_11_minus_10": paired,
        "window_size": 50,
        "early_late": windows,
        "spike_threshold_ms": 200,
        "spikes": [
            {key: row.get(key) for key in ("block", "arm", "index", "request_id", "started_at", PRIMARY)}
            for row in rows
            if _finite(row.get(PRIMARY)) and row[PRIMARY] > 200
        ],
        "leave_one_block_out_sensitivity_ms": sensitivity,
        "note": "All outliers retained. Early/late windows overlap when fewer than 100 samples; sensitivity does not replace official results.",
    }


def _union(intervals: list[tuple[int, int]]) -> int:
    duration, right = 0, 0
    for start, end in sorted(intervals):
        duration += max(0, end - max(start, right))
        right = max(right, end)
    return duration


def _process_nuisance(event: dict) -> bool:
    """GC/lag parent IDs describe inherited context, not lexical nesting."""
    return (event.get("category"), event.get("name")) in (
        ("gc", "gc_collection"),
        ("event_loop", "event_loop_lag"),
    )


def _validate_diagnostic_boundaries(
    row: dict, envelope: dict, setup: list[dict], bodies: list[dict], errors: list[str]
) -> None:
    """Match both sidecars' shared method clock, including repeated output calls."""
    prefix = f"diagnostic request {row.get('request_id')}"
    origin = envelope.get("start_ns")
    spans = row.get("component_intervals_ms")
    if not _integer(origin) or not isinstance(spans, list) or not spans:
        errors.append(f"{prefix}: missing method timing boundary")
        return
    expected = defaultdict(list)
    for span in spans:
        if (
            not isinstance(span, dict)
            or not isinstance(span.get("node_id"), str)
            or not all(_finite(span.get(key)) for key in ("start_ms", "end_ms"))
        ):
            errors.append(f"{prefix}: malformed primary method boundary")
            return
        expected[span["node_id"]].append((span["start_ms"], span["end_ms"]))
    actual = defaultdict(list)
    for body in bodies:
        if not isinstance(body.get("node_id"), str) or not all(
            _integer(body.get(key)) for key in ("start_ns", "end_ns")
        ):
            errors.append(f"{prefix}: malformed diagnostic method boundary")
            continue
        actual[body["node_id"]].append(((body["start_ns"] - origin) / 1000000, (body["end_ns"] - origin) / 1000000))
    if bodies:
        matches = set(actual) == set(expected) and all(
            len(actual[node]) == len(expected[node])
            and all(
                math.isclose(observed, reference, abs_tol=0.000001, rel_tol=0)
                for actual_span, expected_span in zip(sorted(actual[node]), sorted(expected[node]), strict=True)
                for observed, reference in zip(actual_span, expected_span, strict=True)
            )
            for node in expected
        )
        if not matches:
            errors.append(f"{prefix}: component body boundaries/multiplicity differ from primary method spans")
    first_method_ms = min(start for intervals in expected.values() for start, _ in intervals)
    for phase in setup:
        if (
            not all(_integer(phase.get(key)) for key in ("start_ns", "end_ns"))
            or phase["start_ns"] != origin
            or not math.isclose((phase["end_ns"] - origin) / 1000000, first_method_ms, abs_tol=0.000001, rel_tol=0)
        ):
            errors.append(f"{prefix}: pre-component boundary differs from envelope/first method entry")


def _validate_diagnostics(
    manifest: dict,
    rows: list[dict],
    evidence: list[dict],
    events: list[dict],
    metadata: list[dict],
    read_errors: list[str],
) -> dict:
    raw_declaration = manifest.get("diagnostics")
    declaration = _object(raw_declaration)
    if raw_declaration is None or declaration.get("enabled") is False:
        return {"requested": False, "valid": True, "status": "unavailable", "errors": [], "reason": "not_requested"}
    if not isinstance(raw_declaration, dict) or declaration.get("enabled") is not True:
        return {
            "requested": True,
            "valid": False,
            "status": "invalid",
            "errors": ["diagnostics: invalid enabled declaration"],
        }
    errors = list(read_errors)
    if (
        declaration.get("version") != 1
        or declaration.get("detail") not in ("coarse", "setup")
        or not _integer(declaration.get("max_events"), 1)
        or not _finite(declaration.get("lag_interval_ms"))
    ):
        errors.append("diagnostics: unsupported or invalid declaration")
    required = declaration.get("required_capabilities", ["setup", "components", "gc", "event_loop_lag"])
    if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
        errors.append("diagnostics: invalid required_capabilities")
        required = []
    schedule = manifest.get("schedule")
    expected = (
        {_slot(item) for item in schedule if isinstance(item, dict) and _slot(item)}
        if isinstance(schedule, list)
        else set()
    )
    by_slot = defaultdict(list)
    identities = {}
    for item in evidence:
        snapshot = _object(item.get("after_measurement"))
        identities[_slot(item)] = (snapshot.get("pid"), snapshot.get("process_create_time"))
        if _slot(item) in expected:
            identities_at_checkpoints = [
                (_object(item.get(name)).get("pid"), _object(item.get(name)).get("process_create_time"))
                for name in SNAPSHOTS
            ]
            if (
                any(not _integer(pid, 1) or not _finite(created) for pid, created in identities_at_checkpoints)
                or len(set(identities_at_checkpoints)) != 1
            ):
                errors.append(f"diagnostic slot {_slot(item)}: missing/changed worker create time in snapshots")
    for item in metadata:
        slot = _slot(item)
        if slot not in expected or item.get("experiment_id") != manifest.get("experiment_id"):
            errors.append("diagnostic metadata: unknown slot or experiment")
        else:
            by_slot[slot].append(_object(item.get("metadata", item)))
    capabilities = {}
    for slot in expected:
        items = by_slot[slot]
        if not items:
            errors.append(f"diagnostic slot {slot}: missing metadata")
            continue
        for item in items:
            if (
                item.get("enabled") is not True
                or item.get("detail") != declaration.get("detail")
                or item.get("clock") != "worker_monotonic"
                or item.get("unit") != "ns"
            ):
                errors.append(f"diagnostic slot {slot}: metadata configuration/clock mismatch")
            pid, created = identities.get(slot, (None, None))
            if (
                item.get("pid") != pid
                or not _integer(pid, 1)
                or not _finite(item.get("process_create_time"))
                or (created is not None and item.get("process_create_time") != created)
            ):
                errors.append(f"diagnostic slot {slot}: worker process identity mismatch")
            for name in ("dropped", "truncated"):
                if item.get(name) != 0:
                    errors.append(f"diagnostic slot {slot}: missing/nonzero {name} count")
            if (
                not _integer(item.get("unfinished"))
                or not _integer(item.get("drained"))
                or not _integer(item.get("total_drained"))
            ):
                errors.append(f"diagnostic slot {slot}: invalid bounded collector counts")
            elif item["drained"] > item["total_drained"]:
                errors.append(f"diagnostic slot {slot}: drained count exceeds cumulative count")
            if (
                _integer(declaration.get("max_events"), 1)
                and _integer(item.get("drained"))
                and item["drained"] > declaration["max_events"]
            ):
                errors.append(f"diagnostic slot {slot}: drain exceeds declared buffer limit")
            capability = _object(item.get("capabilities"))
            capabilities[str(slot)] = capability
            for name in required:
                if capability.get(name) is not True:
                    errors.append(f"diagnostic slot {slot}: required capability {name} unavailable")
        if items[-1].get("unfinished") != 0:
            errors.append(f"diagnostic slot {slot}: final unfinished events")
    known_requests = {row["request_id"]: row for row in rows if isinstance(row.get("request_id"), str)}
    event_ids = {}
    slot_events = defaultdict(list)
    request_events = defaultdict(list)
    for number, event in enumerate(events, 1):
        prefix = f"diagnostic event {number}"
        slot = _slot(event)
        identity = (slot, event.get("event_id"))
        pid, created = identities.get(slot, (None, None))
        if (
            slot not in expected
            or event.get("experiment_id") != manifest.get("experiment_id")
            or event.get("schema_version") != 1
        ):
            errors.append(f"{prefix}: invalid slot/experiment/schema")
        if (
            event.get("pid") != pid
            or not _integer(pid, 1)
            or not _finite(event.get("process_create_time"))
            or (created is not None and event.get("process_create_time") != created)
        ):
            errors.append(f"{prefix}: worker process identity mismatch")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id or identity in event_ids:
            errors.append(f"{prefix}: missing/duplicate event_id")
            continue
        event_ids[identity] = event
        start, end = event.get("start_ns"), event.get("end_ns")
        if not _integer(start) or not _integer(end) or end < start or not _integer(event.get("thread_id"), 1):
            errors.append(f"{prefix}: invalid monotonic interval/thread identity")
            continue
        if (
            not isinstance(event.get("category"), str)
            or not isinstance(event.get("name"), str)
            or not isinstance(event.get("outcome"), str)
        ):
            errors.append(f"{prefix}: missing event category/name/outcome")
        request_id = event.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str):
                errors.append(f"{prefix}: invalid request reference")
                continue
            row = known_requests.get(request_id)
            if not row or _slot(row) != slot or row.get("pid") != pid:
                errors.append(f"{prefix}: unknown/mismatched request reference")
            request_events[request_id].append(event)
        slot_events[slot].append(event)
    for (slot, _), event in event_ids.items():
        parent_id = event.get("parent_id")
        if parent_id is not None:
            if not isinstance(parent_id, str):
                errors.append("diagnostic events: invalid parent reference")
                continue
            parent = event_ids.get((slot, parent_id))
            if (
                not parent
                or parent is event
                or parent.get("request_id") != event.get("request_id")
                or parent.get("pid") != event.get("pid")
                or parent.get("process_create_time") != event.get("process_create_time")
            ):
                errors.append("diagnostic events: missing/cross-process/cross-request/cyclic parent")
                continue
            if (
                not _process_nuisance(event)
                and all(
                    _integer(value)
                    for value in (
                        event.get("start_ns"),
                        event.get("end_ns"),
                        parent.get("start_ns"),
                        parent.get("end_ns"),
                    )
                )
                and not (parent["start_ns"] <= event["start_ns"] <= event["end_ns"] <= parent["end_ns"])
            ):
                errors.append("diagnostic events: child interval outside parent")
            seen = {event.get("event_id")}
            while parent is not None:
                if parent.get("event_id") in seen:
                    errors.append("diagnostic events: cyclic parent hierarchy")
                    break
                seen.add(parent.get("event_id"))
                ancestor = parent.get("parent_id")
                parent = event_ids.get((slot, ancestor)) if isinstance(ancestor, str) else None
    for slot in expected:
        items = by_slot[slot]
        if items:
            drained = sum(item.get("drained", 0) for item in items if _integer(item.get("drained")))
            # Accept a final cumulative snapshot or every individual drain.
            observed = len(slot_events[slot])
            if items[-1].get("total_drained") != observed or (len(items) > 1 and drained != observed):
                errors.append(f"diagnostic slot {slot}: drained count does not match events")
    covered = 0
    phase_samples = defaultdict(list)
    arm_phase_samples = defaultdict(lambda: defaultdict(list))
    block_phase_samples = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    correlations = []
    for request_id, row in known_requests.items():
        selected = request_events[request_id]
        envelope = [
            event for event in selected if event.get("category") == "request" and event.get("name") == "asgi_request"
        ]
        setup = [
            event for event in selected if event.get("category") == "setup" and event.get("name") == "pre_component"
        ]
        components = {
            event["node_id"]
            for event in selected
            if event.get("category") == "component"
            and event.get("name") == "component_body"
            and isinstance(event.get("node_id"), str)
        }
        spans = row.get("component_intervals_ms")
        needed = (
            {span["node_id"] for span in spans if isinstance(span, dict) and isinstance(span.get("node_id"), str)}
            if isinstance(spans, list)
            else set()
        )
        if (
            len(envelope) != 1
            or ("setup" in required and len(setup) != 1)
            or ("components" in required and components != needed)
        ):
            errors.append(f"diagnostic request {request_id}: incomplete request/setup/component coverage")
            continue
        covered += 1
        interval = envelope[0]
        if not all(_integer(interval.get(key)) for key in ("start_ns", "end_ns")):
            continue
        if _finite(row.get("server_total_ms")) and not math.isclose(
            (interval["end_ns"] - interval["start_ns"]) / 1000000, row["server_total_ms"], abs_tol=0.1, rel_tol=1e-5
        ):
            errors.append(f"diagnostic request {request_id}: envelope differs from primary response boundary")
        bodies = [
            event
            for event in selected
            if event.get("category") == "component" and event.get("name") == "component_body"
        ]
        _validate_diagnostic_boundaries(row, interval, setup, bodies, errors)
        for event in selected:
            if (
                _process_nuisance(event)
                and all(_integer(event.get(key)) for key in ("start_ns", "end_ns"))
                and not (interval["start_ns"] <= event["start_ns"] <= event["end_ns"] <= interval["end_ns"])
            ):
                errors.append(f"diagnostic request {request_id}: nuisance interval outside ASGI envelope")
        if row.get("phase") == "measured":
            phases = defaultdict(list)
            for event in selected:
                if not _process_nuisance(event) and _integer(event.get("start_ns")) and _integer(event.get("end_ns")):
                    phases[f"{event.get('category')}/{event.get('name')}"].append((event["start_ns"], event["end_ns"]))
            for phase, intervals in phases.items():
                sample = {"duration_ms": _union(intervals) / 1000000}
                phase_samples[phase].append(sample)
                arm_phase_samples[row.get("arm")][phase].append(sample)
                block_phase_samples[str(row.get("block"))][row.get("arm")][phase].append(sample)
            nuisance = [
                event
                for event in slot_events[_slot(row)]
                if _process_nuisance(event)
                and max(event["start_ns"], interval["start_ns"]) < min(event["end_ns"], interval["end_ns"])
            ]
            correlations.append(
                {
                    "request_id": request_id,
                    "block": row.get("block"),
                    "arm": row.get("arm"),
                    "spike": _finite(row.get(PRIMARY)) and row[PRIMARY] > 200,
                    "overlap_event_ids": [event["event_id"] for event in nuisance],
                    "gc_count": sum(event.get("category") == "gc" for event in nuisance),
                    "lag_count": sum(event.get("category") == "event_loop" for event in nuisance),
                    "scope": "whole_worker_process",
                    "overlap_rule": "positive_timestamp_intersection",
                }
            )
    return {
        "requested": True,
        "valid": not errors,
        "status": "measured" if not errors else "invalid",
        "errors": errors,
        "event_count": len(events),
        "coverage": {"requests": covered, "expected_requests": len(rows)},
        "capabilities": capabilities,
        "phase_summary": {}
        if errors
        else {name: _stats(samples, "duration_ms") for name, samples in phase_samples.items()},
        "correlations": correlations,
        "phase_summary_by_arm": {}
        if errors
        else {
            arm: {name: _stats(samples, "duration_ms") for name, samples in phases.items()}
            for arm, phases in arm_phase_samples.items()
        },
        "phase_summary_by_block": {}
        if errors
        else {
            block: {
                arm: {name: _stats(samples, "duration_ms") for name, samples in phases.items()}
                for arm, phases in arms.items()
            }
            for block, arms in block_phase_samples.items()
        },
        "source_refs": {
            "events": "diagnostic_events.jsonl",
            "metadata": "diagnostic_metadata.jsonl",
            "experiment_id": manifest.get("experiment_id"),
            "profile_id": manifest.get("profile_id"),
            "detail": declaration.get("detail"),
            "observer_enabled": True,
            "collector_manifest": "manifest.json",
            "collector_source_sha256": _object(manifest.get("source")).get("sha256"),
            "analyzer": {
                "name": "benchmark_analyst.reporting",
                "semantics_version": 2,
                "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "nuisance_parent_relation": "context_provenance",
            },
        },
        "note": "Per-name lexical interval union; categories may overlap and must not be summed. Exact gc/gc_collection and event_loop/event_loop_lag events use contextual parent links and are excluded from phase unions. Nuisance correlation uses positive timestamp intersection across the whole worker process, not request-exclusive time or causal attribution.",
    }


def _contrasts(arms: dict) -> dict:
    means = {arm: arms[arm][PRIMARY]["mean"] for arm in ARMS}

    def compare(enabled: str, baseline: str) -> dict:
        before, after = means[baseline], means[enabled]
        return {"delta_ms": after - before, "reduction_pct": (before - after) / before * 100 if before else None}

    return {
        "metric": PRIMARY,
        "vs_00": {arm: compare(arm, "00") for arm in ARMS[1:]},
        "conditional": {
            "compilation_when_warm_off": compare("10", "00"),
            "compilation_when_warm_on": compare("11", "01"),
            "warm_when_compilation_off": compare("01", "00"),
            "warm_when_compilation_on": compare("11", "10"),
        },
        "interaction_ms": means["11"] - means["10"] - means["01"] + means["00"],
        "average_compilation_effect_ms": (means["10"] - means["00"] + means["11"] - means["01"]) / 2,
        "average_warm_effect_ms": (means["01"] - means["00"] + means["11"] - means["10"]) / 2,
    }


def _tradeoffs(arms: dict, blocks: dict, resources: dict, latency_valid: bool) -> dict:
    if not latency_valid or not resources["valid"] or not resources["requested"] or "00" not in arms:
        return {}
    result = {}
    baseline = arms["00"][PRIMARY]["mean"]
    for arm, checkpoints in _object(resources.get("contrasts")).get("vs_00", {}).items():
        rss = checkpoints["after_idle"]["rss"]
        latency = arms[arm][PRIMARY]["mean"]
        saved = baseline - latency if baseline is not None and latency is not None else None
        increase = rss["mean"] / 1048576 if rss["mean"] is not None else None
        signs = list(rss.get("by_block", {}).values())
        unstable = bool(signs) and min(signs) < 0 < max(signs)
        ratio = (
            saved / increase
            if saved is not None
            and increase is not None
            and increase > 0
            and rss["n"] == 4
            and all(value > 0 for value in signs)
            else None
        )
        result[arm] = {
            "overhead_saved_ms": saved,
            "ram_increase_mib": increase,
            "ms_saved_per_added_mib": ratio,
            "mixed_block_signs": unstable,
            "block_points": [
                {
                    "block": block,
                    "ram_increase_mib": value / 1048576,
                    "overhead_saved_ms": blocks[block]["00"][PRIMARY]["mean"] - blocks[block][arm][PRIMARY]["mean"],
                }
                for block, value in rss.get("by_block", {}).items()
            ],
        }
    return result


def _number(value: Any) -> str:
    return "—" if value is None else f"{value:.3f}"


def _table(headers: list[str], records: list[list]) -> str:
    def clean(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines.extend("| " + " | ".join(clean(value) for value in row) + " |" for row in records)
    return "\n".join(lines)


def _report_markdown(result: dict) -> str:
    manifest = result["manifest"]
    source = _object(manifest.get("source"))
    source_summary = {key: value for key, value in source.items() if key not in {"files", "file_sha256"}}
    source_summary["recorded_file_count"] = len(_object(source.get("files", source.get("file_sha256"))))
    status = "VALID" if result["overall_valid"] else "INVALID"
    scope = (
        "DIAGNOSTIC — paired 10/11, không phải full factorial campaign"
        if result["kind"] == "diagnostic"
        else "CAMPAIGN — 1.000 request đo/arm"
        if result["is_full_campaign"]
        else "SMOKE / cấu hình thử — không phải campaign 1.000 request/arm"
    )
    lines = [
        "# Overhead điều phối Langflow / SCRFD",
        "",
        f"**{status} · {scope}**",
        "",
        f"Experiment: {manifest.get('experiment_id', 'không có')}",
        "",
        BOUNDARY,
        "",
        f"Ranh giới ghi trong manifest: {manifest.get('measurement_boundary', 'không khai báo')}",
        "",
        "## Tính hợp lệ",
        "",
    ]
    if result["valid"]:
        lines.append("Đủ số mẫu theo manifest; output, timing, cờ và counter trong worker đều đạt kiểm tra.")
    else:
        lines.extend(
            [
                "**Không kết luận tăng tốc từ dữ liệu INVALID.** Các thống kê dưới đây chỉ phục vụ chẩn đoán; "
                "không loại lỗi rồi coi phần còn lại là campaign hợp lệ.",
                "",
            ]
        )
        lines.extend(f"- {message}" for message in result["validation_errors"])
    lines.extend(
        [
            "",
            f"Latency/cache/output: {'VALID' if result['valid'] else 'INVALID'}; "
            f"RAM: {result['resource_validity']['status']}; diagnostic observer: {result['diagnostic_validity']['status']}; "
            f"overall: {status}.",
        ]
    )
    for extension in ("resource_validity", "diagnostic_validity"):
        lines.extend(f"- {message}" for message in result[extension].get("errors", []))
    lines.extend(
        [
            "",
            "## Thiết kế và cách đọc",
            "",
            "Arm có bit thứ nhất = compilation cache, bit thứ hai = warm graph registry; 1 bật, 0 tắt. "
            "Legacy HTTP-reuse flag tắt; requests.Session vẫn reuse TCP. Mỗi slot dùng một worker độc lập, concurrency 1. "
            f"Thiết kế khai báo {manifest.get('requests_per_arm', '—')} mẫu đo/arm, bốn block, "
            f"{manifest.get('warmups', '—')} warmup/slot. Warmup không đi vào thống kê độ trễ.",
            "",
            "p50/p95/p99 dùng nội suy tuyến tính (Hyndman-Fan type 7). Mỗi request có trọng số bằng nhau; "
            "các block cân bằng nên mean tổng hợp cũng bằng mean của các mean block. "
            "Các percentile mô tả phân phối mẫu, không phải khoảng tin cậy.",
            "",
            "## Chỉ số chính: Langflow overhead (ms)",
            "",
        ]
    )
    table = []
    for arm in result["arms"]:
        summary, stat = result["arms"][arm], result["arms"][arm][PRIMARY]
        table.append(
            [
                arm,
                summary["attempted"],
                summary["successful"],
                stat["n"],
                *[_number(stat[key]) for key in ("mean", "p50", "p95", "p99")],
            ]
        )
    lines.extend(
        [
            _table(["Arm", "Đã thử", "Output hợp lệ", "n timing", "Mean", "p50", "p95", "p99"], table),
            "",
            "![Phân phối, block và chuỗi request của overhead](charts.png)",
            "",
        ]
    )
    lines.extend(_resource_markdown(result))
    lines.extend(_diagnosis_markdown(result))
    if result["contrasts"]:
        contrasts = result["contrasts"]
        records = [
            [f"{arm} - 00", _number(value["delta_ms"]), _number(value["reduction_pct"])]
            for arm, value in contrasts["vs_00"].items()
        ]
        labels = {
            "compilation_when_warm_off": "Compilation khi warm OFF (10 - 00)",
            "compilation_when_warm_on": "Compilation khi warm ON (11 - 01)",
            "warm_when_compilation_off": "Warm khi compilation OFF (01 - 00)",
            "warm_when_compilation_on": "Warm khi compilation ON (11 - 10)",
        }
        records.extend(
            [labels[key], _number(value["delta_ms"]), _number(value["reduction_pct"])]
            for key, value in contrasts["conditional"].items()
        )
        lines.extend(
            [
                "## Hiệu ứng trên mean overhead",
                "",
                "Δ = mean bật - mean đối chứng: âm nghĩa là overhead thấp hơn trong lần chạy này. "
                "Giảm (%) = (đối chứng - bật) / đối chứng x 100; âm nghĩa là chậm hơn. "
                "Không mặc định cache hoặc kết hợp hai cache luôn nhanh hơn.",
                "",
                _table(["So sánh", "Δ ms", "Giảm %"], records),
                "",
                f"Tương tác μ11 - μ10 - μ01 + μ00 = **{_number(contrasts['interaction_ms'])} ms**. "
                "Giá trị âm cho thấy mức giảm kết hợp lớn hơn tổng hai mức giảm riêng theo thang ms.",
                "",
                "Không báo CI hay p-value. Chỉ có bốn block độc lập; 4.000 request không phải "
                "4.000 lần lặp độc lập của điều kiện máy. Kết quả là quan sát tại workload, source và máy đã ghi nhận.",
                "",
            ]
        )
    lines.extend(["## Theo block (chỉ số chính, ms)", ""])
    records = [
        [block, arm, summary[PRIMARY]["n"], *[_number(summary[PRIMARY][key]) for key in ("mean", "p50", "p95", "p99")]]
        for block, arms in result["blocks"].items()
        for arm, summary in arms.items()
    ]
    lines.extend(
        [
            _table(["Block", "Arm", "n", "Mean", "p50", "p95", "p99"], records),
            "",
            "## Chỉ số phụ (ms)",
            "",
            "server_total và SCRFD dùng để kiểm tra ranh giới phép trừ. flow_api gồm thời gian phía client "
            "quanh API /run; upload được ghi riêng và không cộng vào overhead.",
            "",
        ]
    )
    records = [
        [arm, metric, summary[metric]["n"], *[_number(summary[metric][key]) for key in ("mean", "p50", "p95", "p99")]]
        for arm, summary in result["arms"].items()
        for metric in METRICS[1:]
    ]
    lines.extend(
        [
            _table(["Arm", "Metric", "n", "Mean", "p50", "p95", "p99"], records),
            "",
            "## Bằng chứng worker",
            "",
            "Counter bên dưới là after_measurement - after_warmup. Warm ON phải hit ở mọi request đo "
            "và không cold; OFF phải cold ở mọi request và không hit. Compilation ON phải có hit đo được; "
            "OFF phải có bypass và không hit. PID/cờ phải nhất quán trước warmup, sau warmup và sau đo.",
            "",
        ]
    )
    columns = [
        "block",
        "arm",
        "pid",
        "measured_attempts",
        "compilation_hits",
        "compilation_bypasses",
        "warm_hits",
        "warm_cold",
    ]
    columns.extend(
        key
        for key in (
            "compilation_misses",
            "compilation_builds",
            "compilation_evictions",
            "warm_misses",
            "warm_builds",
            "warm_evictions",
            "compilation_hit_rate",
        )
        if any(key in item and item[key] is not None for item in result["worker_diagnostics"])
    )
    lines.extend(
        [
            _table(columns, [[item.get(key, "—") for key in columns] for item in result["worker_diagnostics"]]),
            "",
            "Hit rate dùng compilation hits/(hits+misses), chỉ hiển thị khi có counter miss; compilation events không phải số request. "
            "Cache bị reset theo worker/slot; capacity/occupancy/payload được ghi riêng dưới đây. Payload bytes là source UTF-8, AST pickle hoặc resident JSON, không phải heap bytes hoặc RAM quy cho từng cache.",
            "",
            "```json",
            json.dumps(
                [
                    {
                        "block": item["block"],
                        "arm": item["arm"],
                        "cache_occupancy": item["cache_occupancy"],
                        "effective": item["effective"],
                    }
                    for item in result["worker_diagnostics"]
                ],
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            "## Nguồn và khả năng tái lập",
            "",
            "manifest.json giữ thiết kế, hashes workload/source; requests.jsonl giữ mọi attempt; "
            "worker_evidence.jsonl giữ snapshot worker. Báo cáo đọc offline và không sửa ba file raw. "
            "Thiếu, sai số lượng, lỗi output/timing hoặc thiếu bằng chứng làm toàn bộ run INVALID.",
            "",
            "```json",
            json.dumps(
                {
                    "workload": manifest.get("workload"),
                    "source": source_summary,
                    "profile_id": manifest.get("profile_id"),
                    "profile": manifest.get("profile"),
                    "fixture": manifest.get("fixture"),
                    "raw_sha256": result["raw_sha256"],
                },
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _resource_markdown(result: dict) -> list[str]:
    resource = result["resource_validity"]
    lines = [
        "## Trade-off latency và RAM worker",
        "",
        "Phạm vi một flow SCRFD, một serving worker, concurrency 1. RSS/USS là RAM toàn worker; chưa đo peak RAM. "
        "before_warmup là sau readiness và workload verification, không phải process hoàn toàn cold. "
        "after_idle là sau ít nhất thời gian idle khai báo; background tasks vẫn chạy và checkpoint không chứng minh quiescence.",
        "",
        "Không có SLA hoặc ngân sách RAM để chọn winner chung. Mean/median và p95/p99 là các mục tiêu khác nhau; "
        "đọc chênh lệch theo block trước khi xếp hạng. Payload cache không đo deep heap và không quy bytes RAM cho từng cache.",
        "",
    ]
    if not resource["requested"]:
        return lines + [
            "RAM chưa đo (legacy / measurement disabled); RSS và USS unavailable, không chuyển thành zero.",
            "",
        ]
    lines.extend(
        [
            f"RAM coverage/status: {resource['status']}. USS unavailable hoặc bị AccessDenied có reason riêng và không thay bằng zero.",
            "",
        ]
    )
    metrics = _object(resource.get("metrics"))
    lines.extend(
        [
            _table(
                ["Metric", "Status", "n", "Expected", "Unavailable reasons"],
                [
                    [
                        metric,
                        item.get("status"),
                        item.get("n"),
                        item.get("expected"),
                        json.dumps(item.get("unavailable_reasons", []), ensure_ascii=False),
                    ]
                    for metric, item in metrics.items()
                ],
            ),
            "",
        ]
    )
    table = []
    for arm, checkpoints in resource.get("arms", {}).items():
        latency = result["arms"].get(arm, {}).get(PRIMARY, {})
        for checkpoint, memory in checkpoints.items():
            table.append(
                [
                    arm,
                    checkpoint,
                    *[_number(latency.get(key)) for key in ("mean", "p50", "p95", "p99")],
                    *[
                        _number(_object(memory.get(metric)).get("mean") / 1048576)
                        if _object(memory.get(metric)).get("mean") is not None
                        else "—"
                        for metric in ("rss", "uss")
                    ],
                ]
            )
    lines.extend(
        [_table(["Arm", "Checkpoint", "Mean ms", "p50 ms", "p95 ms", "p99 ms", "RSS MiB", "USS MiB"], table), ""]
    )
    contrasts = _object(resource.get("contrasts"))
    table = []
    comparisons = [(f"{arm} - 00", checkpoints) for arm, checkpoints in contrasts.get("vs_00", {}).items()]
    if contrasts.get("marginal_11_10"):
        comparisons.append(("11 - 10", contrasts["marginal_11_10"]))
    for label, checkpoints in comparisons:
        for checkpoint, metric_values in checkpoints.items():
            for metric, values in metric_values.items():
                table.append(
                    [
                        label,
                        checkpoint,
                        metric,
                        _number(values.get("mean") / 1048576) if values.get("mean") is not None else "—",
                        json.dumps(
                            {
                                block: value / 1048576 if value is not None else None
                                for block, value in values.get("by_block", {}).items()
                            }
                        ),
                    ]
                )
    lines.extend(
        [
            "ΔRAM = arm - đối chứng trong cùng block/checkpoint; âm là hợp lệ. n RAM = số worker, tối đa 4/arm; không phải số request. "
            "Difference-in-differences dùng (after_idle - before_warmup) giữa arm và đối chứng.",
            "",
            _table(["So sánh", "Checkpoint", "Metric", "Δ MiB", "Theo block (MiB)"], table),
            "",
        ]
    )
    if contrasts.get("difference_in_differences"):
        lines.extend(
            [
                "```json",
                json.dumps(
                    {"difference_in_differences_bytes": contrasts["difference_in_differences"]},
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
            ]
        )
    if result.get("has_tradeoff_figure"):
        lines.extend(
            [
                _table(
                    [
                        "Arm vs 00",
                        "Overhead saved ms",
                        "After-idle RSS Δ MiB",
                        "ms saved / added MiB",
                        "Block signs mixed",
                    ],
                    [
                        [
                            arm,
                            _number(item["overhead_saved_ms"]),
                            _number(item["ram_increase_mib"]),
                            _number(item["ms_saved_per_added_mib"]),
                            item["mixed_block_signs"],
                        ]
                        for arm, item in result["tradeoffs"].items()
                    ],
                ),
                "",
                "Nếu ΔRAM đổi dấu giữa blocks, chưa đủ bằng chứng xếp hạng RAM; không dùng tỷ số để che biến động. Tỷ số chỉ có khi cả bốn block tăng RSS dương.",
                "",
            ]
        )
        lines.extend(
            [
                "Overhead saved = mean00 - mean_arm; RAM tăng = RSS_arm - RSS00. Tỷ số ms/MiB chỉ mô tả khi ΔRSS dương và coverage đủ, không phải điểm tối ưu chung.",
                "",
                "![RAM increase versus overhead saved, with block points](tradeoffs.png)",
                "",
            ]
        )
    return lines


def _diagnosis_markdown(result: dict) -> list[str]:
    decomposition = result["setup_decomposition"]
    tail = result["tail_diagnostics"]
    manifest = result["manifest"]
    lines = [
        "## Quan sát 11 so với 10 và phần chưa giải thích",
        "",
        f"Phân rã method spans: {decomposition['status']}; {len(decomposition['records'])} request có đủ sáu span tuần tự. "
        "pre + gaps + post + Chat Input/Output = overhead. Missing/overlap chỉ làm phân rã unavailable; metric union SCRFD vẫn được kiểm tra riêng. "
        "Đoạn pre gồm middleware/auth/flow/setup/dispatch, chưa phải bằng chứng quy cho deepcopy.",
        "",
    ]
    table = [
        [
            block,
            *[_number(item[key]) for key in ("mean", "p50", "p95", "p99")],
            *[_number(item["segments"][key]) for key in ("pre_ms", "gaps_ms", "post_ms", "chat_ms")],
        ]
        for block, item in tail["paired_11_minus_10"].items()
    ]
    lines.extend(
        [
            _table(["Block", "Δmean", "Δp50", "Δp95", "Δp99", "Δpre", "Δgaps", "Δpost", "Δchat"], table),
            "",
            "Giữ mọi outlier. Spike là overhead >200 ms; không retry hoặc loại mẫu. Early/late là 50 request đầu/cuối mỗi slot; "
            "với slot dưới 100 request hai cửa sổ overlap. Leave-one-block-out chỉ là sensitivity, không thay official results.",
            "",
            _table(
                ["Block", "Arm", "Early n", "Early mean", "Late n", "Late mean", "Overlap"],
                [
                    [
                        block,
                        arm,
                        item["early"]["n"],
                        _number(item["early"]["mean"]),
                        item["late"]["n"],
                        _number(item["late"]["mean"]),
                        item["overlap"],
                    ]
                    for block, arms in tail["early_late"].items()
                    for arm, item in arms.items()
                ],
            ),
            "",
            _table(
                ["Block", "Arm", "Index", "Request ID", "Overhead ms"],
                [
                    [item.get(key) for key in ("block", "arm", "index", "request_id", PRIMARY)]
                    for item in tail["spikes"]
                ],
            ),
            "",
            f"Sensitivity 11-10 mean khi bỏ từng block (ms): {json.dumps(tail['leave_one_block_out_sensitivity_ms'])}",
            "",
        ]
    )
    if result.get("has_diagnostic_figure"):
        lines.extend(
            [
                f"Nguồn figure: run {manifest.get('experiment_id')}; profile {manifest.get('profile_id', 'legacy/unavailable')}; "
                f"detail {_object(manifest.get('diagnostics')).get('detail', 'method-spans only')}; observer enabled={_object(manifest.get('diagnostics')).get('enabled', False)}. "
                "Các quan sát diagnostic không được gộp vào campaign khác hoặc trừ khỏi primary metric.",
                "",
                "![Method envelope decomposition and spike timeline](diagnostics.png)",
                "",
            ]
        )
    diagnostic = result["diagnostic_validity"]
    if diagnostic["requested"]:
        lines.extend(
            [
                f"Observer coverage: {diagnostic['status']}; capabilities: {json.dumps(diagnostic.get('capabilities', {}), ensure_ascii=False)}. "
                "Event durations nested dùng interval union, không cộng parent/child. GC/lag overlap là tương quan, chưa chứng minh nguyên nhân.",
                "",
                "Nguồn: [diagnostic events](diagnostic_events.jsonl), [diagnostic metadata](diagnostic_metadata.jsonl); [analysis](analysis.json) giữ coverage và correlations.",
                "",
            ]
        )
        phase_rows = [
            [arm, name, stat["n"], *[_number(stat[key]) for key in ("mean", "p50", "p95", "p99")]]
            for arm, phases in diagnostic.get("phase_summary_by_arm", {}).items()
            for name, stat in phases.items()
        ]
        lines.extend(
            [
                _table(["Arm", "Observer phase (union ms)", "n", "Mean", "p50", "p95", "p99"], phase_rows),
                "",
                "Các phase khác tên có thể overlap; không cộng các hàng thành total. Capability unavailable ngăn quy nguyên nhân cho DB/telemetry/background. "
                "Phân rã và overlap hiện là observed; explained chỉ khi có thí nghiệm đối chứng phù hợp, phần còn lại unresolved.",
                "",
            ]
        )
    lines.extend(
        [
            "## Chi phí warmup",
            "",
            _table(
                ["Arm", "Warmup attempts", "Tổng server time warmup (ms)"],
                [[arm, item["n"], _number(item["total_server_ms"])] for arm, item in result["warmup_cost"].items()],
            ),
            "",
            "Warmup được ghi riêng và không tham gia latency steady-state. Tổng server time warmup chưa gồm startup, readiness, upload/download hoặc verification; "
            "chưa đủ startup cost đối chứng để suy break-even request count.",
            "",
        ]
    )
    return lines


def _report_html(markdown: str) -> str:
    """Render our small report vocabulary without trusting raw text as HTML."""
    output = []
    table_open = False
    code_open = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            output.append("</pre>" if code_open else "<pre>")
            code_open = not code_open
            continue
        if code_open:
            output.append(html.escape(line) + "\n")
            continue
        if line.startswith("| "):
            cells = line.strip("| ").split(" | ")
            if all(cell == "---" for cell in cells):
                continue
            if not table_open:
                output.append('<div class="table-wrap"><table>')
                tag = "th"
                table_open = True
            else:
                tag = "td"
            output.append("<tr>" + "".join(f"<{tag}>{html.escape(cell)}</{tag}>" for cell in cells) + "</tr>")
            continue
        if table_open:
            output.append("</table></div>")
            table_open = False
        if line.startswith("!["):
            match = re.fullmatch(r"!\[([^\]]*)\]\((charts\.png|tradeoffs\.png|diagnostics\.png)\)", line)
            if match:
                output.append(f'<img src="{match[2]}" alt="{html.escape(match[1], quote=True)}">')
        elif line.startswith("## "):
            output.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            output.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line:
            escaped = html.escape(line)
            escaped = re.sub(
                r"\[([^\]]+)\]\((manifest\.json|requests\.jsonl|worker_evidence\.jsonl|memory\.jsonl|diagnostic_events\.jsonl|diagnostic_metadata\.jsonl|analysis\.json)\)",
                lambda match: f'<a href="{match[2]}">{match[1]}</a>',
                escaped,
            )
            parts = escaped.split("**")
            escaped = "".join(
                ("<strong>" if i % 2 else "</strong>") + part if i else part for i, part in enumerate(parts)
            )
            output.append(f"<p>{escaped}</p>")
    if table_open:
        output.append("</table></div>")
    return (
        """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Langflow / SCRFD overhead</title><style>
body{font:16px/1.6 system-ui,sans-serif;color:#172b3a;background:#f5f7fa;margin:0}
main{max-width:1100px;margin:auto;padding:36px;background:white}
h1,h2{line-height:1.25}h2{margin-top:2.2em;border-top:1px solid #dce4eb;padding-top:1em}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:14px}
th,td{padding:8px 12px;border-bottom:1px solid #dce4eb;text-align:right;white-space:nowrap}
th{background:#edf3f7}td:first-child,th:first-child{text-align:left}
.table-wrap{overflow:auto}img{width:100%;height:auto}pre{overflow:auto;padding:18px;background:#eef3f7;font-size:13px}
strong{color:#173f5f}@media print{main{padding:0}body{background:white}h2{break-after:avoid}tr{break-inside:avoid}}
</style></head><body><main>"""
        + "\n".join(output)
        + "</main></body></html>\n"
    )


def _charts(path: Path, result: dict, rows: list[dict]) -> None:
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"00": "#64748b", "01": "#0891b2", "10": "#7c3aed", "11": "#059669"}
    fig, axes = plt.subplots(3, 1, figsize=(11, 12), layout="constrained")
    scope = (
        "DIAGNOSTIC paired 10/11"
        if result["kind"] == "diagnostic"
        else "1,000 measured requests/arm"
        if result["is_full_campaign"]
        else "SMOKE / trial configuration"
    )
    title = (
        f"Latency VALID | {scope} | RAM {result['resource_validity']['status']} | overall {'VALID' if result['overall_valid'] else 'INVALID'}"
        if result["valid"]
        else "Latency INVALID — diagnostic only; no speedup claims"
    )
    fig.suptitle(f"Langflow orchestration overhead\n{title}", fontsize=15, weight="bold")
    arm_names = tuple(result["arms"])
    for position, arm in enumerate(arm_names):
        stat = result["arms"][arm][PRIMARY]
        for offset, key in enumerate(("mean", "p50", "p95", "p99")):
            if stat[key] is not None:
                axes[0].bar(
                    position + (offset - 1.5) * 0.18,
                    stat[key],
                    width=0.17,
                    color=colors[arm],
                    alpha=1 - 0.16 * offset,
                    label=key if position == 0 else None,
                )
        block_points = [
            (int(block), arms[arm][PRIMARY]["mean"])
            for block, arms in result["blocks"].items()
            if arms[arm][PRIMARY]["mean"] is not None
        ]
        if block_points:
            axes[1].plot(
                [point[0] for point in block_points],
                [point[1] for point in block_points],
                "o-",
                color=colors[arm],
                label=arm,
            )
        samples = [
            row
            for row in rows
            if row.get("arm") == arm
            and row.get("phase") == "measured"
            and row.get("outcome") == "success"
            and row.get("output_valid") is True
            and _finite(row.get(PRIMARY))
        ]
        if samples:
            axes[2].plot(
                range(1, len(samples) + 1),
                [row[PRIMARY] for row in samples],
                ".",
                ms=3,
                alpha=0.6,
                color=colors[arm],
                label=arm,
            )
    axes[0].set_xticks(range(len(arm_names)), [f"{arm}\nn={result['arms'][arm][PRIMARY]['n']}" for arm in arm_names])
    axes[0].set_title("Measured requests: mean and percentiles")
    axes[1].set(title="Block means: inspect order / machine drift", xlabel="Block", xticks=[1, 2, 3, 4])
    axes[2].set(title="Measured request sequence within each arm", xlabel="Attempt sequence (warmups excluded)")
    for axis in axes:
        axis.set_ylabel("Overhead (ms)")
        axis.set_ylim(bottom=0)
        axis.grid(axis="y", alpha=0.2)
        handles, labels = axis.get_legend_handles_labels()
        if handles:
            axis.legend(handles, labels, loc="upper right", ncol=4)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def _extra_charts(run_dir: Path, result: dict, rows: list[dict]) -> None:
    import matplotlib.pyplot as plt

    obsolete = []
    if not result["has_tradeoff_figure"]:
        obsolete.extend(("tradeoffs.png", "resource_summary.csv"))
    if not result["has_diagnostic_figure"]:
        obsolete.append("diagnostics.png")
    for name in obsolete:
        path = run_dir / name
        if path.is_file() or path.is_symlink():
            path.unlink()
    colors = {"00": "#64748b", "01": "#0891b2", "10": "#7c3aed", "11": "#059669"}
    resource = result["resource_validity"]
    if result["has_tradeoff_figure"]:
        fig, axes = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
        checkpoints = ("before_warmup", "after_warmup", "after_measurement", "after_idle")
        for arm, data in resource["arms"].items():
            means = [data[checkpoint]["rss"]["mean"] for checkpoint in checkpoints]
            axes[0].plot(
                range(4),
                [value / 1048576 if value is not None else math.nan for value in means],
                "o-",
                label=arm,
                color=colors[arm],
            )
        plotted = False
        for block, arms in resource.get("blocks", {}).items():
            if "00" not in arms:
                continue
            baseline_ram = arms["00"]["after_idle"]["rss"]["mean"]
            baseline_latency = result["blocks"][block]["00"][PRIMARY]["mean"]
            for arm in arms:
                ram, latency = arms[arm]["after_idle"]["rss"]["mean"], result["blocks"][block][arm][PRIMARY]["mean"]
                if arm == "00" or any(value is None for value in (ram, latency, baseline_ram, baseline_latency)):
                    continue
                x, y = (ram - baseline_ram) / 1048576, baseline_latency - latency
                axes[1].scatter(x, y, color=colors[arm])
                axes[1].annotate(
                    f"B{block}/{arm}", (x, y), xytext=(4, (int(block) - 2) * 9), textcoords="offset points", fontsize=8
                )
                plotted = True
        axes[0].set(
            xticks=range(4),
            xticklabels=["before\nwarmup", "after\nwarmup", "after\nmeasurement", "after\nidle"],
            ylabel="Whole worker RSS (MiB)",
            title="Four checkpoints; peak RAM not measured",
        )
        axes[0].legend()
        axes[1].set(
            xlabel="After-idle RSS increase vs 00 (MiB)",
            ylabel="Mean overhead saved vs 00 (ms)",
            title="Signed block points; no universal winner",
        )
        if not plotted:
            axes[1].text(0.5, 0.5, "Paired 10/11: no arm 00 baseline", transform=axes[1].transAxes, ha="center")
        axes[1].axhline(0, color="#64748b", lw=0.8)
        axes[1].axvline(0, color="#64748b", lw=0.8)
        axes[1].margins(x=0.15, y=0.15)
        fig.suptitle(f"{result['manifest'].get('experiment_id')} | RAM {resource['status']} | whole serving worker")
        fig.savefig(run_dir / "tradeoffs.png", dpi=140)
        plt.close(fig)
        with (run_dir / "resource_summary.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "scope",
                    "block",
                    "arm",
                    "checkpoint",
                    "metric",
                    "unit",
                    "n",
                    "mean",
                    "p50",
                    "p95",
                    "p99",
                    "resource_valid",
                ]
            )
            scopes = [
                ("arm", "all", resource["arms"]),
                *(("block", block, arms) for block, arms in resource.get("blocks", {}).items()),
            ]
            for scope, block, arms in scopes:
                for arm, checkpoints in arms.items():
                    for checkpoint, metrics in checkpoints.items():
                        for metric, stat in metrics.items():
                            writer.writerow(
                                [
                                    scope,
                                    block,
                                    arm,
                                    checkpoint,
                                    metric,
                                    "bytes",
                                    *[stat[key] for key in ("n", "mean", "p50", "p95", "p99")],
                                    resource["valid"],
                                ]
                            )
    if result["has_diagnostic_figure"]:
        fig, axes = plt.subplots(2, 1, figsize=(11, 8), layout="constrained")
        decomposition = result["setup_decomposition"]
        names = ("pre_ms", "gaps_ms", "post_ms", "chat_ms")
        for position, arm in enumerate(PAIRED_ARMS):
            values = [decomposition["arms"][arm][name]["mean"] for name in names]
            bottom = 0
            for name, value, color in zip(names, values, ("#7c3aed", "#0891b2", "#64748b", "#059669"), strict=True):
                if value is not None:
                    axes[0].bar(position, value, bottom=bottom, label=name if position == 0 else None, color=color)
                    bottom += value
        axes[0].set(
            xticks=range(2),
            xticklabels=PAIRED_ARMS,
            ylabel="Mean overhead segment (ms)",
            title="Sequential method envelope; pre is not graph-copy time",
        )
        axes[0].legend(ncol=4)
        for arm in PAIRED_ARMS:
            samples = [row for row in rows if row.get("arm") == arm and _finite(row.get(PRIMARY))]
            axes[1].plot(
                range(len(samples)), [row[PRIMARY] for row in samples], ".", color=colors[arm], ms=3, label=arm
            )
        axes[1].axhline(200, color="#dc2626", lw=0.8, label="spike >200 ms")
        axes[1].set(
            xlabel="Measured attempt sequence within each arm",
            ylabel="Overhead (ms)",
            title="All samples retained; correlated events are not causal attribution",
        )
        axes[1].legend()
        declaration = _object(result["manifest"].get("diagnostics"))
        fig.suptitle(
            f"Source: {result['manifest'].get('experiment_id')} | profile: {result['manifest'].get('profile_id', 'legacy')}\nDetail: {declaration.get('detail', 'method spans')} | observer enabled: {declaration.get('enabled', False)} | coverage: {result['diagnostic_validity']['status']}"
        )
        fig.savefig(run_dir / "diagnostics.png", dpi=140)
        plt.close(fig)


def analyze(run_dir: Path) -> dict:
    """Validate raw evidence and write JSON, CSV, Markdown, HTML and PNG reports.

    A structurally valid smoke run is distinguishable from a 1,000-request-per-arm
    campaign. Invalid datasets retain descriptive diagnostics but have no effects.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        message = f"Run directory does not exist: {run_dir}"
        raise FileNotFoundError(message)
    errors: list[str] = []
    manifest = _read_json(run_dir / "manifest.json", errors)
    state = None
    if (run_dir / "state.json").exists():
        state = _read_json(run_dir / "state.json", errors)
        if state.get("status") != "COMPLETE":
            errors.append("state.json: runner did not complete final workload/source integrity checks")
    rows = _read_jsonl(run_dir / "requests.jsonl", errors)
    evidence = _read_jsonl(run_dir / "worker_evidence.jsonl", errors)
    expected = _validate_manifest(manifest, errors)
    grouped = _validate_rows(manifest, rows, expected, errors)
    worker_diagnostics = _validate_evidence(evidence, expected, grouped, errors)
    from benchmark_analyst.campaign_contract import validate_effective_profile

    validate_effective_profile(manifest, evidence, errors)
    measured = [row for row in rows if row.get("phase") == "measured"]
    arm_names = _arms(manifest)
    arms = {arm: _summarize([row for row in measured if row.get("arm") == arm]) for arm in arm_names}
    blocks = {
        str(block): {
            arm: _summarize([row for row in grouped[(block, arm)] if row.get("phase") == "measured"])
            for arm in arm_names
        }
        for block in range(1, 5)
    }
    from benchmark_analyst.memory_metrics import validate_resources

    resource_errors = []
    memory = (
        _read_jsonl(run_dir / "memory.jsonl", resource_errors)
        if _object(manifest.get("resource_measurement")).get("enabled") is True
        else []
    )
    resource_validity = validate_resources(manifest, memory, evidence)
    if resource_errors:
        resource_validity["errors"].extend(resource_errors)
        resource_validity.update(valid=False, status="invalid", contrasts=None)
    diagnostic_errors = []
    diagnostic_enabled = _object(manifest.get("diagnostics")).get("enabled") is True
    events = _read_jsonl(run_dir / "diagnostic_events.jsonl", diagnostic_errors) if diagnostic_enabled else []
    metadata = _read_jsonl(run_dir / "diagnostic_metadata.jsonl", diagnostic_errors) if diagnostic_enabled else []
    diagnostic_validity = _validate_diagnostics(manifest, rows, evidence, events, metadata, diagnostic_errors)
    decomposition = _decomposition(manifest, measured)
    kind = manifest.get("kind", "factorial")
    result = {
        "schema_version": 1,
        "kind": kind,
        "valid": not errors,
        "overall_valid": not errors and resource_validity["valid"] and diagnostic_validity["valid"],
        "resource_validity": resource_validity,
        "diagnostic_validity": diagnostic_validity,
        "validation_errors": errors,
        "is_full_campaign": kind == "factorial"
        and manifest.get("requests_per_arm") == CAMPAIGN_REQUESTS_PER_ARM
        and manifest.get("blocks") == BLOCK_COUNT
        and set(expected) == {(block, arm) for block in range(1, 5) for arm in ARMS},
        "primary_metric": PRIMARY,
        "boundary": BOUNDARY,
        "manifest": manifest,
        "state": state,
        "arms": arms,
        "blocks": blocks,
        "worker_diagnostics": worker_diagnostics,
        "contrasts": _contrasts(arms) if not errors and kind == "factorial" else None,
        "setup_decomposition": decomposition,
        "tail_diagnostics": _tail_diagnostics(measured, blocks, decomposition),
        "warmup_cost": {
            arm: {
                "n": len(samples := [row for row in rows if row.get("arm") == arm and row.get("phase") == "warmup"]),
                "total_server_ms": sum(
                    row["server_total_ms"] for row in samples if _finite(row.get("server_total_ms"))
                ),
            }
            for arm in arm_names
        },
        "raw_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                run_dir / name
                for name in (
                    "manifest.json",
                    "requests.jsonl",
                    "worker_evidence.jsonl",
                    "state.json",
                    "memory.jsonl",
                    "diagnostic_events.jsonl",
                    "diagnostic_metadata.jsonl",
                )
            )
            if path.is_file()
        },
        "has_tradeoff_figure": not errors
        and resource_validity["valid"]
        and resource_validity["requested"]
        and bool(resource_validity.get("arms")),
        "has_diagnostic_figure": bool(decomposition["records"]),
        "tradeoffs": _tradeoffs(arms, blocks, resource_validity, not errors),
        "statistical_note": "Descriptive results; four independent blocks; no CI or significance claim.",
    }
    # Never emit NaN/Infinity in derived JSON, including malformed manifest metadata.
    result = json.loads(json.dumps(result, ensure_ascii=False), parse_constant=lambda _: None)
    (run_dir / "analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    with (run_dir / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["scope", "block", "arm", "metric", "n", "mean", "p50", "p95", "p99", "valid", "attempted", "successful"]
        )
        scopes = [("arm", "all", arms), *(("block", block, summaries) for block, summaries in blocks.items())]
        for scope, block, summaries in scopes:
            for arm, summary in summaries.items():
                for metric in METRICS:
                    writer.writerow(
                        [
                            scope,
                            block,
                            arm,
                            metric,
                            *[summary[metric][key] for key in ("n", "mean", "p50", "p95", "p99")],
                            result["valid"],
                            summary["attempted"],
                            summary["successful"],
                        ]
                    )
    markdown = _report_markdown(result)
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    (run_dir / "report.html").write_text(_report_html(markdown), encoding="utf-8")
    _charts(run_dir / "charts.png", result, rows)
    _extra_charts(run_dir, result, measured)
    return result
