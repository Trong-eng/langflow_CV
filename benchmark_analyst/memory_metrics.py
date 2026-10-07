"""External worker RAM checkpoints and offline, paired resource summaries.

psutil is imported only when a collector is constructed. RSS/USS describe the
whole worker; cache accounting describes retained payloads and is not heap size.
"""

from __future__ import annotations

import copy
import importlib
import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import fmean
from typing import Any

CHECKPOINTS = ("before_warmup", "after_warmup", "after_measurement", "after_idle")
METRICS = ("rss", "uss")
IDLE_SECONDS = 5


def resource_declaration() -> dict:
    """Return a new v1 declaration for the runner's four RAM checkpoints."""
    return {
        "version": 1,
        "enabled": True,
        "required": ["rss"],
        "optional": ["uss"],
        "checkpoints": list(CHECKPOINTS),
        "idle_seconds": IDLE_SECONDS,
    }


def _integer(value: Any, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _finite(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0 else None


def _failure(reason: str) -> dict:
    return {"bytes": None, "status": "unavailable", "reason": reason, "started_at_utc": _utc(), "duration_ms": 0.0}


class MemoryCollector:
    """Pin the serving worker identified by its authenticated snapshot.

    Callers fetch identity/accounting before calling collect and keep the last
    run_sample completion on the runner's monotonic clock. No requests or idle
    waits occur inside this collector.
    """

    def __init__(self, experiment_id: str, block: int, arm: str, snapshot: dict):
        if (
            not isinstance(experiment_id, str)
            or not experiment_id
            or not _integer(block, 1)
            or arm not in {"00", "01", "10", "11"}
        ):
            raise ValueError("invalid experiment or worker slot")
        if not isinstance(snapshot, dict) or not _integer(snapshot.get("pid"), 1):
            raise ValueError("authenticated serving worker PID is missing or invalid")
        self.psutil = importlib.import_module("psutil")
        self.process = self.psutil.Process(snapshot["pid"])
        self.pid = snapshot["pid"]
        self.process_create_time = self.process.create_time()
        supplied = snapshot.get("process_create_time")
        if not _finite(self.process_create_time) or (supplied is not None and supplied != self.process_create_time):
            raise ValueError("authenticated worker process identity does not match psutil")
        self.experiment_id, self.block, self.arm = experiment_id, block, arm
        self._snapshot = copy.deepcopy(snapshot)
        self._checkpoint_index = 0
        self._uss_unavailable_reason: str | None = None

    def _identity_error(self, snapshot: dict) -> str | None:
        if not _integer(snapshot.get("pid"), 1) or snapshot["pid"] != self.pid:
            return "process_identity_changed"
        if snapshot.get("process_create_time", self.process_create_time) != self.process_create_time:
            return "process_identity_changed"
        try:
            # psutil caches create_time; is_running also detects reuse of a PID.
            running = getattr(self.process, "is_running", None)
            if (running is not None and not running()) or self.process.create_time() != self.process_create_time:
                return "process_identity_changed"
        except Exception as exc:  # An OS failure is evidence, never a dropped checkpoint.
            return type(exc).__name__
        return None

    def _measure(self, metric: str) -> dict:
        started, clock = _utc(), time.perf_counter()
        try:
            value = getattr(self.process.memory_info() if metric == "rss" else self.process.memory_full_info(), metric)
            if not _integer(value):
                raise ValueError("noninteger or negative process memory")
            result = {"bytes": value, "status": "measured", "reason": None}
        except Exception as exc:
            result = {"bytes": None, "status": "unavailable", "reason": type(exc).__name__}
            if metric == "uss" and isinstance(exc, (AttributeError, NotImplementedError, PermissionError)):
                self._uss_unavailable_reason = result["reason"]
            if metric == "uss" and type(exc).__name__ == "AccessDenied":
                self._uss_unavailable_reason = result["reason"]
        return {**result, "started_at_utc": started, "duration_ms": (time.perf_counter() - clock) * 1000}

    @staticmethod
    def _accounting(snapshot: dict, started: str) -> dict:
        accounting = snapshot.get("cache_accounting")
        if isinstance(accounting, dict) and isinstance(accounting.get("snapshot"), dict):
            return copy.deepcopy(accounting)
        if not isinstance(accounting, dict):
            accounting = {
                "compilation": snapshot.get("compilation_accounting", snapshot.get("compilation", {})),
                "warm": snapshot.get("warm_accounting", {"entries": snapshot.get("registry_entries")}),
            }
        return {
            "started_at_utc": snapshot.get("snapshot_started_at_utc", started),
            "duration_ms": snapshot.get("snapshot_duration_ms", 0.0),
            "snapshot": copy.deepcopy(accounting),
        }

    def collect(
        self, checkpoint: str, *, snapshot: dict | None = None, last_sample_finished: float | None = None
    ) -> dict:
        """Read RSS then optional USS, preserving failed attempts as raw records."""
        if self._checkpoint_index >= len(CHECKPOINTS) or checkpoint != CHECKPOINTS[self._checkpoint_index]:
            raise ValueError("checkpoint must follow the declared order exactly once")
        current = self._snapshot if snapshot is None else snapshot
        if not isinstance(current, dict):
            raise ValueError("authenticated snapshot must be an object")
        started = _utc()
        elapsed = None if last_sample_finished is None else (time.monotonic() - last_sample_finished) * 1000
        accounting = self._accounting(current, started)
        identity_error = self._identity_error(current)
        rss = _failure(identity_error) if identity_error else self._measure("rss")
        if identity_error:
            uss = _failure(identity_error)
        elif self._uss_unavailable_reason:
            uss = _failure(self._uss_unavailable_reason)
        else:
            uss = self._measure("uss")
        identity_error = identity_error or self._identity_error(current)
        if identity_error:
            rss.update(bytes=None, status="unavailable", reason=identity_error)
            uss.update(bytes=None, status="unavailable", reason=identity_error)
        self._checkpoint_index += 1
        self._snapshot = copy.deepcopy(current)
        return {
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "block": self.block,
            "arm": self.arm,
            "checkpoint": checkpoint,
            "pid": self.pid,
            "process_create_time": self.process_create_time,
            "elapsed_since_last_sample_ms": elapsed,
            "started_at_utc": started,
            "finished_at_utc": _utc(),
            "collector": {"name": "psutil", "version": getattr(self.psutil, "__version__", "unavailable")},
            "rss": rss,
            "uss": uss,
            "cache_accounting": accounting,
        }


def _stats(values: list[int]) -> dict:
    values = sorted(values)
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None}

    def percentile(fraction: float) -> float:
        position = (len(values) - 1) * fraction
        left, right = math.floor(position), math.ceil(position)
        return values[left] + (values[right] - values[left]) * (position - left)

    return {
        "n": len(values),
        "mean": fmean(values),
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
    }


def _slot(item: dict) -> tuple:
    return item.get("block"), item.get("arm")


def _metric_value(row: dict, name: str) -> int | None:
    metric = row.get(name)
    if isinstance(metric, dict) and metric.get("status") == "measured" and _integer(metric.get("bytes")):
        return metric["bytes"]
    return None


def _validate_timing(item: dict, label: str, errors: list[str]) -> datetime | None:
    started = _timestamp(item.get("started_at_utc"))
    if started is None or not _finite(item.get("duration_ms")):
        errors.append(f"{label}: invalid timestamp or duration")
    return started


def _measurement_end(started: datetime | None, duration: Any, label: str, errors: list[str]) -> datetime | None:
    if started is None or not _finite(duration):
        return None
    try:
        return started + timedelta(milliseconds=duration)
    except OverflowError:
        errors.append(f"{label}: duration exceeds timestamp range")
        return None


def _validate_record(row: dict, manifest: dict, label: str, errors: list[str]) -> None:
    if row.get("schema_version") != 1 or isinstance(row.get("schema_version"), bool):
        errors.append(f"{label}: invalid schema_version")
    experiment_id = row.get("experiment_id")
    if (
        not isinstance(experiment_id, str)
        or not experiment_id.strip()
        or experiment_id != manifest.get("experiment_id")
    ):
        errors.append(f"{label}: wrong experiment_id")
    if not _integer(row.get("pid"), 1) or not _finite(row.get("process_create_time")):
        errors.append(f"{label}: invalid process identity")
    collector = row.get("collector")
    if (
        not isinstance(collector, dict)
        or collector.get("name") != "psutil"
        or not isinstance(collector.get("version"), str)
        or not collector["version"]
    ):
        errors.append(f"{label}: missing collector name or version")
    record_start, record_end = _timestamp(row.get("started_at_utc")), _timestamp(row.get("finished_at_utc"))
    if record_start is None or record_end is None or record_end < record_start:
        errors.append(f"{label}: invalid checkpoint collection interval")
    elapsed = row.get("elapsed_since_last_sample_ms")
    if elapsed is not None and not _finite(elapsed):
        errors.append(f"{label}: invalid elapsed_since_last_sample_ms")
    if row.get("checkpoint") == "after_idle" and (not _finite(elapsed) or elapsed < IDLE_SECONDS * 1000):
        errors.append(f"{label}: after_idle begins before the required idle duration")
    if row.get("checkpoint") == "before_warmup" and elapsed is not None:
        errors.append(f"{label}: before_warmup must precede the first sample")
    if row.get("checkpoint") == "after_measurement" and not _finite(elapsed):
        errors.append(f"{label}: after_measurement requires the last sample completion")
    starts, ends = {}, {}
    for name in METRICS:
        metric = row.get(name)
        if not isinstance(metric, dict):
            errors.append(f"{label}: missing {name} measurement")
            continue
        starts[name] = _validate_timing(metric, f"{label}/{name}", errors)
        ends[name] = _measurement_end(starts[name], metric.get("duration_ms"), f"{label}/{name}", errors)
        if ends[name]:
            if (record_start and starts[name] < record_start) or (record_end and ends[name] > record_end):
                errors.append(f"{label}/{name}: memory read outside checkpoint collection")
        status, value, reason = metric.get("status"), metric.get("bytes"), metric.get("reason")
        if status == "measured":
            if not _integer(value) or reason is not None:
                errors.append(f"{label}/{name}: measured bytes must be a nonnegative integer with no failure reason")
        elif status == "unavailable":
            if value is not None or not isinstance(reason, str) or not reason:
                errors.append(f"{label}/{name}: unavailable measurement needs null bytes and a reason")
        else:
            errors.append(f"{label}/{name}: invalid measurement status")
        if name == "rss" and status != "measured":
            errors.append(f"{label}: mandatory RSS unavailable")
    if ends.get("rss") and starts.get("uss") and starts["uss"] < ends["rss"]:
        errors.append(f"{label}: RSS must precede USS")
    accounting = row.get("cache_accounting")
    if not isinstance(accounting, dict) or not isinstance(accounting.get("snapshot"), dict):
        errors.append(f"{label}: missing cache accounting snapshot")
    else:
        accounting_start = _validate_timing(accounting, f"{label}/cache_accounting", errors)
        accounting_end = _measurement_end(
            accounting_start, accounting.get("duration_ms"), f"{label}/cache_accounting", errors
        )
        if accounting_end and starts.get("rss") and accounting_end > starts["rss"]:
            errors.append(f"{label}: accounting snapshot must precede RSS")


def _summaries(expected: list[tuple], grouped: dict) -> tuple[dict, dict]:
    arms, blocks = {}, {}
    for block, arm in expected:
        arms.setdefault(arm, {checkpoint: {name: [] for name in METRICS} for checkpoint in CHECKPOINTS})
        block_arm = blocks.setdefault(str(block), {}).setdefault(arm, {})
        by_checkpoint = {row.get("checkpoint"): row for row in grouped[(block, arm)]}
        for checkpoint in CHECKPOINTS:
            row = by_checkpoint.get(checkpoint, {})
            block_arm[checkpoint] = {}
            for name in METRICS:
                value = _metric_value(row, name)
                values = [] if value is None else [value]
                block_arm[checkpoint][name] = _stats(values)
                arms[arm][checkpoint][name].extend(values)
    for checkpoints in arms.values():
        for metrics in checkpoints.values():
            for name, values in metrics.items():
                metrics[name] = _stats(values)
    return arms, blocks


def _contrasts(blocks: dict, arms: dict, grouped: dict) -> dict:
    raw = {(str(block), arm, row["checkpoint"]): row for (block, arm), rows in grouped.items() for row in rows}

    def contrast(arm: str, baseline: str, checkpoint: str | None, metric: str) -> dict:
        differences = {}
        for block, members in blocks.items():
            if arm not in members or baseline not in members:
                continue
            phases = (checkpoint,) if checkpoint else ("before_warmup", "after_idle")
            values = [
                _metric_value(raw[(block, member, phase)], metric) for member in (arm, baseline) for phase in phases
            ]
            if any(value is None for value in values):
                continue
            delta = values[0] - values[1] if checkpoint else (values[1] - values[0]) - (values[3] - values[2])
            differences[block] = delta
        return {**_stats(list(differences.values())), "by_block": differences}

    vs_00 = {}
    did_vs_00 = {}
    if "00" in arms:
        for arm in arms:
            if arm == "00":
                continue
            vs_00[arm] = {
                phase: {metric: contrast(arm, "00", phase, metric) for metric in METRICS} for phase in CHECKPOINTS
            }
            did_vs_00[arm] = {metric: contrast(arm, "00", None, metric) for metric in METRICS}
    marginal, did_marginal = {}, {}
    if {"10", "11"}.issubset(arms):
        marginal = {phase: {metric: contrast("11", "10", phase, metric) for metric in METRICS} for phase in CHECKPOINTS}
        did_marginal = {metric: contrast("11", "10", None, metric) for metric in METRICS}
    return {
        "vs_00": vs_00,
        "marginal_11_10": marginal,
        "difference_in_differences": {"vs_00": did_vs_00, "marginal_11_10": did_marginal},
    }


def validate_resources(manifest: dict, records: list[dict], evidence: list[dict]) -> dict:
    """Validate declared RAM independently of historical latency validity.

    Absolute summaries and signed contrasts are bytes. Each checkpoint contributes
    one serving worker per block, never one repetition per request.
    """
    declaration = manifest.get("resource_measurement")
    base = {
        "requested": False,
        "valid": True,
        "status": "unavailable",
        "reason": "not_requested",
        "errors": [],
        "records": records,
        "metrics": {},
        "arms": {},
        "blocks": {},
        "contrasts": None,
    }
    if declaration is None or (isinstance(declaration, dict) and declaration.get("enabled") is False):
        return base
    errors: list[str] = []
    experiment_id = manifest.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        errors.append("resources: missing or invalid experiment_id")
    if declaration != resource_declaration():
        errors.append("resource_measurement: unsupported or invalid v1 declaration")
    schedule = manifest.get("schedule")
    expected = []
    if not isinstance(schedule, list) or not schedule:
        errors.append("resources: missing worker schedule")
    else:
        for item in schedule:
            if (
                not isinstance(item, dict)
                or not _integer(item.get("block"), 1)
                or not isinstance(item.get("arm"), str)
                or item["arm"] not in {"00", "01", "10", "11"}
            ):
                errors.append("resources: invalid scheduled slot")
                continue
            slot = _slot(item)
            if slot in expected:
                errors.append(f"resources: duplicate scheduled slot {slot}")
            else:
                expected.append(slot)
    grouped = defaultdict(list)
    order = []
    for index, row in enumerate(records):
        if not isinstance(row, dict):
            errors.append(f"memory record {index}: expected object")
            continue
        slot = _slot(row)
        if not _integer(row.get("block"), 1) or not isinstance(row.get("arm"), str) or slot not in expected:
            errors.append(f"memory record {index}: unknown worker slot")
            continue
        if not isinstance(row.get("checkpoint"), str) or row["checkpoint"] not in CHECKPOINTS:
            errors.append(f"memory record {index}: invalid checkpoint")
            continue
        grouped[slot].append(row)
        order.append((slot, row.get("checkpoint")))
        _validate_record(row, manifest, f"memory slot {slot}/{row.get('checkpoint')}", errors)
    wanted_order = [(slot, checkpoint) for slot in expected for checkpoint in CHECKPOINTS]
    if order != wanted_order:
        errors.append("resources: checkpoint order or coverage does not match scheduled workers")
    evidence_slots = defaultdict(list)
    for item in evidence:
        if (
            isinstance(item, dict)
            and _integer(item.get("block"), 1)
            and isinstance(item.get("arm"), str)
            and _slot(item) in expected
        ):
            evidence_slots[_slot(item)].append(item)
    seen_workers = {}
    for slot in expected:
        rows = grouped[slot]
        if Counter(row.get("checkpoint") for row in rows) != Counter(CHECKPOINTS):
            errors.append(f"memory slot {slot}: requires exactly four unique checkpoints")
        identities = [(row.get("pid"), row.get("process_create_time")) for row in rows]
        if identities and any(identity != identities[0] for identity in identities):
            errors.append(f"memory slot {slot}: process identity changed (PID reuse or wrong worker)")
        if identities and _integer(identities[0][0], 1) and _finite(identities[0][1]):
            identity = identities[0]
            if identity in seen_workers:
                errors.append(f"memory slot {slot}: serving process reused from slot {seen_workers[identity]}")
            seen_workers[identity] = slot
        previous_end = None
        for row in rows:
            current_start, current_end = _timestamp(row.get("started_at_utc")), _timestamp(row.get("finished_at_utc"))
            if previous_end and current_start and current_start < previous_end:
                errors.append(f"memory slot {slot}: checkpoint collection intervals out of order")
            previous_end = current_end
        workers = evidence_slots[slot]
        if len(workers) != 1:
            errors.append(f"memory slot {slot}: expected one worker evidence record")
            continue
        worker = workers[0]
        snapshots = [worker.get(phase) for phase in CHECKPOINTS if phase in worker]
        if not all(phase in worker for phase in CHECKPOINTS[:3]):
            errors.append(f"memory slot {slot}: missing worker evidence snapshot")
        for snapshot in snapshots:
            if (
                not isinstance(snapshot, dict)
                or not _integer(snapshot.get("pid"), 1)
                or not identities
                or snapshot["pid"] != identities[0][0]
            ):
                errors.append(f"memory slot {slot}: worker evidence PID mismatch")
            elif (
                not _finite(snapshot.get("process_create_time")) or snapshot["process_create_time"] != identities[0][1]
            ):
                errors.append(f"memory slot {slot}: worker evidence process identity mismatch")
    arms, blocks = _summaries(expected, grouped)
    metrics = {}
    for name in METRICS:
        measured = sum(_metric_value(row, name) is not None for row in records if isinstance(row, dict))
        reasons = Counter(
            row[name]["reason"]
            for row in records
            if isinstance(row, dict)
            and isinstance(row.get(name), dict)
            and row[name].get("status") == "unavailable"
            and isinstance(row[name].get("reason"), str)
        )
        metrics[name] = {
            "status": "measured"
            if measured == len(wanted_order) and measured > 0
            else ("partial" if measured else "unavailable"),
            "n": measured,
            "expected": len(wanted_order),
            "unavailable_reasons": dict(reasons),
        }
    return {
        **base,
        "requested": True,
        "valid": not errors,
        "status": "invalid" if errors else "measured",
        "reason": None,
        "errors": errors,
        "metrics": metrics,
        "arms": arms,
        "blocks": blocks,
        "contrasts": None if errors else _contrasts(blocks, arms, grouped),
    }
