"""RAM evidence must stay separate from latency and retain paired worker effects."""
# ruff: noqa: S101, PLR2004, INP001

import copy
import importlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

CHECKPOINTS = ("before_warmup", "after_warmup", "after_measurement", "after_idle")
MIB = 1048576


def memory_module():
    return importlib.import_module("benchmark_analyst.memory_metrics")


def metric(value, second=0, *, status="measured", reason=None):
    return {
        "bytes": value,
        "status": status,
        "reason": reason,
        "started_at_utc": f"2026-10-02T00:00:{second:02d}+00:00",
        "duration_ms": 0.1,
    }


@pytest.fixture
def resources():
    declaration = {
        "version": 1,
        "enabled": True,
        "required": ["rss"],
        "optional": ["uss"],
        "checkpoints": list(CHECKPOINTS),
        "idle_seconds": 5,
    }
    manifest = {
        "schema_version": 1,
        "experiment_id": "ram-fixture",
        "blocks": 4,
        "schedule": [
            {"block": block, "arm": arm, "count": 2} for block in range(1, 5) for arm in ("00", "01", "10", "11")
        ],
        "resource_measurement": declaration,
    }
    rows, evidence = [], []
    before = {"00": 100, "01": 110, "10": 90, "11": 95}
    after = {"00": 120, "01": 115, "10": 115, "11": 112}
    for slot_index, slot in enumerate(manifest["schedule"]):
        block, arm = slot["block"], slot["arm"]
        pid, created = 1000 + slot_index, 100.0 + slot_index
        worker = {**slot}
        for index, checkpoint in enumerate(CHECKPOINTS):
            value = (before[arm] if index == 0 else after[arm]) * MIB + block * MIB
            row = {
                "schema_version": 1,
                "experiment_id": "ram-fixture",
                "block": block,
                "arm": arm,
                "checkpoint": checkpoint,
                "pid": pid,
                "process_create_time": created,
                "elapsed_since_last_sample_ms": None if index < 2 else (0 if index == 2 else 5000),
                "started_at_utc": f"2026-10-02T00:00:{index * 10:02d}+00:00",
                "finished_at_utc": f"2026-10-02T00:00:{index * 10 + 2:02d}+00:00",
                "collector": {"name": "psutil", "version": "7.2.2"},
                "rss": metric(value, index * 10),
                "uss": metric(None, index * 10 + 1, status="unavailable", reason="AccessDenied"),
                "cache_accounting": {
                    "started_at_utc": f"2026-10-02T00:00:{index * 10:02d}+00:00",
                    "duration_ms": 0,
                    "snapshot": {
                        "compilation": {"entries": int(arm[0])},
                        "warm": {"resident_json_bytes": int(arm[1]) * 100},
                    },
                },
            }
            rows.append(row)
            worker[checkpoint] = {"pid": pid, "process_create_time": created}
        evidence.append(worker)
    return manifest, rows, evidence


def test_legacy_does_not_require_ram_or_import_psutil():
    result = memory_module().validate_resources({}, [], [])
    assert result["requested"] is False
    assert result["valid"] is True
    assert result["status"] == "unavailable"
    assert result["reason"] == "not_requested"
    code = "import sys; sys.modules['psutil'] = None; from benchmark_analyst.memory_metrics import validate_resources; assert validate_resources({}, [], [])['valid']"
    completed = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_complete_rss_accepts_optional_uss_denied_and_keeps_raw_immutable(resources):
    manifest, rows, evidence = resources
    frozen = json.dumps([manifest, rows, evidence], sort_keys=True)
    result = memory_module().validate_resources(manifest, rows, evidence)
    assert result["valid"] is True
    assert result["status"] == "measured"
    assert result["errors"] == []
    assert result["arms"]["00"]["before_warmup"]["rss"]["n"] == 4
    assert result["arms"]["00"]["before_warmup"]["rss"]["mean"] == 102.5 * MIB
    assert result["arms"]["00"]["before_warmup"]["uss"]["mean"] is None
    assert result["metrics"]["uss"]["status"] == "unavailable"
    assert json.dumps([manifest, rows, evidence], sort_keys=True) == frozen


def test_signed_same_block_ram_contrasts_and_difference_in_differences(resources):
    result = memory_module().validate_resources(*resources)
    contrasts = result["contrasts"]
    assert contrasts["vs_00"]["10"]["after_idle"]["rss"]["mean"] == -5 * MIB
    assert contrasts["marginal_11_10"]["after_idle"]["rss"]["mean"] == -3 * MIB
    assert contrasts["difference_in_differences"]["vs_00"]["11"]["rss"]["mean"] == -3 * MIB
    assert contrasts["difference_in_differences"]["marginal_11_10"]["rss"]["mean"] == -8 * MIB
    assert contrasts["marginal_11_10"]["after_idle"]["rss"]["by_block"] == {
        str(block): -3 * MIB for block in range(1, 5)
    }
    assert contrasts["marginal_11_10"]["after_idle"]["uss"]["n"] == 0


def test_diagnostic_pair_produces_only_available_contrasts(resources):
    manifest, rows, evidence = resources
    manifest["kind"] = "diagnostic"
    manifest["schedule"] = [slot for slot in manifest["schedule"] if slot["arm"] in {"10", "11"}]
    rows[:] = [row for row in rows if row["arm"] in {"10", "11"}]
    evidence[:] = [row for row in evidence if row["arm"] in {"10", "11"}]
    result = memory_module().validate_resources(manifest, rows, evidence)
    assert result["valid"] is True
    assert result["contrasts"]["vs_00"] == {}
    assert result["contrasts"]["marginal_11_10"]["after_idle"]["rss"]["mean"] == -3 * MIB


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "reordered",
        "wrong_slot",
        "wrong_experiment",
        "wrong_pid",
        "reused_pid",
        "wrong_evidence_pid",
        "missing_evidence",
        "invalid_bytes",
        "bool_bytes",
        "rss_unavailable",
        "short_idle",
        "invalid_schema",
        "invalid_declaration",
        "invalid_timestamp",
        "invalid_duration",
        "metric_order",
        "invalid_record_interval",
        "missing_collector",
        "missing_evidence_create_time",
        "backwards_checkpoints",
        "bool_block",
        "nonnumeric_block",
        "malformed_metric",
        "overlapping_metric_reads",
        "unavailable_without_reason",
        "malformed_checkpoint",
        "malformed_reason",
        "malformed_schedule_arm",
    ],
)
def test_invalid_required_resources_block_ram_conclusions(resources, mutation):
    manifest, rows, evidence = resources
    row = rows[0]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(row))
    elif mutation == "reordered":
        rows[0], rows[1] = rows[1], rows[0]
    elif mutation == "wrong_slot":
        row["arm"] = "99"
    elif mutation == "wrong_experiment":
        row["experiment_id"] = "another-run"
    elif mutation == "wrong_pid":
        row["pid"] = 9999
    elif mutation == "reused_pid":
        row["process_create_time"] += 1
    elif mutation == "wrong_evidence_pid":
        evidence[0]["before_warmup"]["pid"] += 1
    elif mutation == "missing_evidence":
        evidence.pop()
    elif mutation == "invalid_bytes":
        row["rss"]["bytes"] = -1
    elif mutation == "bool_bytes":
        row["rss"]["bytes"] = True
    elif mutation == "rss_unavailable":
        row["rss"].update(bytes=None, status="unavailable", reason="NoSuchProcess")
    elif mutation == "short_idle":
        rows[3]["elapsed_since_last_sample_ms"] = 4999
    elif mutation == "invalid_schema":
        row["schema_version"] = 2
    elif mutation == "invalid_declaration":
        manifest["resource_measurement"]["required"] = []
    elif mutation == "invalid_timestamp":
        row["rss"]["started_at_utc"] = "yesterday"
    elif mutation == "invalid_duration":
        row["rss"]["duration_ms"] = float("nan")
    elif mutation == "metric_order":
        row["uss"]["started_at_utc"] = "2026-10-01T00:00:00+00:00"
    elif mutation == "invalid_record_interval":
        row["finished_at_utc"] = "2026-10-01T00:00:00+00:00"
    elif mutation == "missing_collector":
        row.pop("collector")
    elif mutation == "missing_evidence_create_time":
        evidence[0]["before_warmup"].pop("process_create_time")
    elif mutation == "backwards_checkpoints":
        rows[1]["rss"]["started_at_utc"] = "2026-10-01T00:00:00+00:00"
    elif mutation == "bool_block":
        row["block"] = True
    elif mutation == "nonnumeric_block":
        row["block"] = []
    elif mutation == "malformed_metric":
        row["rss"] = []
    elif mutation == "overlapping_metric_reads":
        row["rss"]["duration_ms"] = 2000
    elif mutation == "unavailable_without_reason":
        row["uss"]["reason"] = None
    elif mutation == "malformed_checkpoint":
        row["checkpoint"] = []
    elif mutation == "malformed_reason":
        row["uss"]["reason"] = {}
    elif mutation == "malformed_schedule_arm":
        manifest["schedule"][0]["arm"] = []
    result = memory_module().validate_resources(manifest, rows, evidence)
    assert result["requested"] is True
    assert result["valid"] is False, mutation
    assert result["status"] == "invalid"
    assert result["errors"], mutation
    assert result["contrasts"] is None


class AccessDenied(Exception):
    pass


class FakeProcess:
    """Only the OS memory/identity boundary is replaced; collection remains real."""

    def __init__(self):
        self.pid = 1234
        self.created = 123.5
        self.calls = []
        self.rss_error = None
        self.uss_error = AccessDenied()
        self.running = True

    def create_time(self):
        return self.created

    def is_running(self):
        return self.running

    def memory_info(self):
        self.calls.append("rss")
        if self.rss_error:
            raise self.rss_error
        return SimpleNamespace(rss=123456)

    def memory_full_info(self):
        self.calls.append("uss")
        if self.uss_error:
            raise self.uss_error
        return SimpleNamespace(uss=65432)


@pytest.fixture
def collector_boundary(monkeypatch):
    process = FakeProcess()

    def serving_process(pid):
        assert pid == 1234
        return process

    monkeypatch.setitem(
        sys.modules, "psutil", SimpleNamespace(Process=serving_process, __version__="test", AccessDenied=AccessDenied)
    )
    snapshot = {
        "pid": 1234,
        "process_create_time": 123.5,
        "cache_accounting": {"compilation": {"entries": 6}, "warm": {"resident_json_bytes": 100}},
    }
    return process, snapshot


def test_collector_records_optional_denial_once_and_reads_rss_first(collector_boundary):
    process, snapshot = collector_boundary
    collector = memory_module().MemoryCollector("test", 1, "11", snapshot)
    first = collector.collect("before_warmup", snapshot=snapshot)
    second = collector.collect("after_warmup", snapshot=snapshot)
    assert first["pid"] == 1234
    assert first["process_create_time"] == 123.5
    assert first["rss"]["bytes"] == 123456
    assert first["uss"]["bytes"] is None
    assert first["uss"]["reason"] == "AccessDenied"
    assert first["uss"]["status"] == "unavailable"
    assert second["uss"]["reason"] == "AccessDenied"
    assert first["elapsed_since_last_sample_ms"] is None
    assert first["cache_accounting"]["snapshot"]["warm"]["resident_json_bytes"] == 100
    assert process.calls == ["rss", "uss", "rss"]
    assert first["rss"]["duration_ms"] >= 0
    assert second["rss"]["duration_ms"] >= 0


def test_collector_uses_separate_uss_result_when_supported(collector_boundary):
    process, snapshot = collector_boundary
    process.uss_error = None
    collector = memory_module().MemoryCollector("test", 1, "11", snapshot)
    first = collector.collect("before_warmup", snapshot=snapshot)
    assert first["rss"]["bytes"] == 123456
    assert first["uss"]["bytes"] == 65432
    assert first["rss"]["status"] == first["uss"]["status"] == "measured"
    assert first["uss"]["started_at_utc"] >= first["rss"]["started_at_utc"]


def test_collector_detects_pid_reuse_without_measuring_new_process(collector_boundary):
    process, snapshot = collector_boundary
    collector = memory_module().MemoryCollector("test", 1, "11", snapshot)
    process.created += 1
    record = collector.collect("before_warmup", snapshot=snapshot)
    assert record["rss"]["status"] == "unavailable"
    assert record["rss"]["reason"] == "process_identity_changed"
    assert record["rss"]["bytes"] is None
    assert process.calls == []


def test_collector_detects_psutil_cached_create_time_reuse(collector_boundary):
    process, snapshot = collector_boundary
    collector = memory_module().MemoryCollector("test", 1, "11", snapshot)
    process.running = False
    record = collector.collect("before_warmup", snapshot=snapshot)
    assert record["rss"]["reason"] == "process_identity_changed"
    assert process.calls == []


def test_collector_rejects_identity_change_during_memory_read(collector_boundary, monkeypatch):
    process, snapshot = collector_boundary
    collector = memory_module().MemoryCollector("test", 1, "11", snapshot)

    def reused_during_read():
        process.created += 1
        return SimpleNamespace(rss=123456)

    monkeypatch.setattr(process, "memory_info", reused_during_read)
    record = collector.collect("before_warmup", snapshot=snapshot)
    assert record["rss"]["bytes"] is None
    assert record["rss"]["reason"] == "process_identity_changed"
    assert record["uss"]["bytes"] is None


def test_collector_retains_accounting_snapshot_measurement_provenance(collector_boundary):
    _, snapshot = collector_boundary
    snapshot.update(snapshot_started_at_utc="2026-10-02T00:00:00+00:00", snapshot_duration_ms=2.0)
    collector = memory_module().MemoryCollector("test", 1, "11", snapshot)
    record = collector.collect("before_warmup", snapshot=snapshot)
    snapshot["cache_accounting"]["warm"]["resident_json_bytes"] = 999
    assert record["cache_accounting"]["started_at_utc"] == "2026-10-02T00:00:00+00:00"
    assert record["cache_accounting"]["duration_ms"] == 2.0
    assert record["cache_accounting"]["snapshot"]["warm"]["resident_json_bytes"] == 100


def test_collector_records_rss_failure_without_dropping_checkpoint(collector_boundary):
    process, snapshot = collector_boundary
    process.rss_error = AccessDenied()
    collector = memory_module().MemoryCollector("test", 1, "11", snapshot)
    record = collector.collect("before_warmup", snapshot=snapshot)
    assert record["checkpoint"] == "before_warmup"
    assert record["rss"]["reason"] == "AccessDenied"
    assert record["rss"]["bytes"] is None


def test_collector_rejects_checkpoint_order_and_wrong_snapshot_identity(collector_boundary):
    _, snapshot = collector_boundary
    collector = memory_module().MemoryCollector("test", 1, "11", snapshot)
    with pytest.raises(ValueError, match="checkpoint"):
        collector.collect("after_idle", snapshot=snapshot)
    record = collector.collect("before_warmup", snapshot={**snapshot, "pid": 9999})
    assert record["rss"]["reason"] == "process_identity_changed"


def test_collector_records_elapsed_from_last_completed_sample(collector_boundary, monkeypatch):
    _, snapshot = collector_boundary
    module = memory_module()
    monkeypatch.setattr(module.time, "monotonic", lambda: 20.25)
    collector = module.MemoryCollector("test", 1, "11", snapshot)
    collector.collect("before_warmup", snapshot=snapshot)
    collector.collect("after_warmup", snapshot=snapshot)
    collector.collect("after_measurement", snapshot=snapshot, last_sample_finished=15)
    record = collector.collect("after_idle", snapshot=snapshot, last_sample_finished=15)
    assert record["elapsed_since_last_sample_ms"] == 5250


def test_collector_rejects_untrusted_identity_before_process_lookup(collector_boundary):
    _, snapshot = collector_boundary
    with pytest.raises(ValueError, match="PID"):
        memory_module().MemoryCollector("test", 1, "11", {**snapshot, "pid": True})
    with pytest.raises(ValueError, match="identity"):
        memory_module().MemoryCollector("test", 1, "11", {**snapshot, "process_create_time": 1})


def test_paired_differences_preserve_integer_bytes_above_float_precision(resources):
    manifest, rows, evidence = resources
    for row in rows:
        row["rss"]["bytes"] = 2**60 + (1 if row["arm"] == "11" else 0)
    result = memory_module().validate_resources(manifest, rows, evidence)
    assert result["contrasts"]["marginal_11_10"]["after_idle"]["rss"]["mean"] == 1


def test_slots_require_fresh_workers_but_allow_os_to_recycle_pid(resources):
    manifest, rows, evidence = resources
    for row in rows[4:8]:
        row["pid"] = rows[0]["pid"]
    for phase in CHECKPOINTS:
        evidence[1][phase]["pid"] = rows[0]["pid"]
    assert memory_module().validate_resources(manifest, rows, evidence)["valid"] is True
    for row in rows[4:8]:
        row["process_create_time"] = rows[0]["process_create_time"]
    for phase in CHECKPOINTS:
        evidence[1][phase]["process_create_time"] = rows[0]["process_create_time"]
    result = memory_module().validate_resources(manifest, rows, evidence)
    assert result["valid"] is False
    assert result["contrasts"] is None


@pytest.mark.parametrize("experiment_id", [None, "", " ", 1, []])
def test_matching_missing_or_malformed_experiment_ids_are_invalid(resources, experiment_id):
    manifest, rows, evidence = resources
    manifest["experiment_id"] = experiment_id
    for row in rows:
        row["experiment_id"] = experiment_id
    result = memory_module().validate_resources(manifest, rows, evidence)
    assert result["valid"] is False
    assert result["contrasts"] is None


@pytest.mark.parametrize("field", ["rss", "uss", "cache_accounting"])
@pytest.mark.parametrize("duration", [1e300, 10**400])
def test_unrepresentable_memory_durations_return_invalid_evidence(resources, field, duration):
    manifest, rows, evidence = resources
    rows[0][field]["duration_ms"] = duration
    result = memory_module().validate_resources(manifest, rows, evidence)
    assert result["valid"] is False
    assert result["errors"]
    assert result["contrasts"] is None
