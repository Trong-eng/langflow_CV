"""Offline validation and paired summaries of the four diagnostic treatments."""

from __future__ import annotations

import hashlib
import html
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from benchmark_analyst import reporting
from benchmark_analyst.campaign_contract import diagnostics_declaration, profile_identity
from benchmark_analyst.memory_metrics import resource_declaration
from benchmark_analyst.protocol import make_diagnostic_schedule

VARIANTS = ("D0", "D1", "D2", "D3")
PAIRED_ARMS = ("10", "11")
STATISTICS = ("mean", "p50", "p95", "p99")
RAW_FILES = (
    "manifest.json",
    "state.json",
    "requests.jsonl",
    "worker_evidence.jsonl",
    "memory.jsonl",
    "diagnostic_events.jsonl",
    "diagnostic_metadata.jsonl",
)
COMPARISONS = {
    "D1-D0": ("D1", "D0", "observer package"),
    "D2-D1": ("D2", "D1", "product telemetry policy"),
    "D3-D1": ("D3", "D1", "warmup policy includes additional DB/job/trace writes"),
}
STATISTICAL_NOTE = (
    "Summary differences pair the same outer block and arm; requests are not independent repetitions. "
    "No CI, p-value, or causal claim. Diagnostic timings remain separate from primary campaign results."
)


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _integer(value: Any, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _read_json(path: Path, errors: list[str], label: str) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"{label}: cannot read JSON ({type(exc).__name__})")
        return {}
    if not isinstance(result, dict):
        errors.append(f"{label}: expected a JSON object")
        return {}
    return result


def _hashes(directory: Path, names: tuple[str, ...]) -> dict:
    hashes = {}
    for name in names:
        try:
            hashes[name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        except OSError:
            hashes[name] = None
    return hashes


def _slot(slot: Any, variant: str | None = None) -> tuple | None:
    if not isinstance(slot, dict) or not _integer(slot.get("block"), 1) or not _integer(slot.get("count"), 1):
        return None
    outer_block = slot.get("outer_block_id", slot["block"])
    if slot.get("arm") not in PAIRED_ARMS or not _integer(outer_block, 1) or outer_block != slot["block"]:
        return None
    treatment = slot.get("variant", variant)
    if treatment not in VARIANTS or (variant is not None and treatment != variant):
        return None
    return slot["block"], slot["block"], treatment, slot["arm"], slot["count"]


def _suite_manifest(suite: dict, errors: list[str]) -> list[dict]:
    if type(suite.get("schema_version")) is not int or suite["schema_version"] != 1:
        errors.append("suite.json: unsupported schema_version")
    if suite.get("kind") != "diagnostic_suite" or suite.get("status") != "COMPLETE":
        errors.append("suite.json: diagnostic suite must be COMPLETE")
    if type(suite.get("blocks")) is not int or suite["blocks"] != 4:
        errors.append("suite.json: requires four outer blocks")
    count = suite.get("requests_per_arm")
    mode = suite.get("mode")
    if not _integer(count, 1) or (mode, count) not in (("smoke", 8), ("campaign", 1000)):
        errors.append("suite.json: requires eight requests/arm for smoke or 1000 for campaign")
        count = 8
    wanted = make_diagnostic_schedule(count)
    schedule = suite.get("schedule")
    if not isinstance(schedule, list) or [_slot(slot) for slot in schedule] != [_slot(slot) for slot in wanted]:
        errors.append("suite.json: schedule must contain 32 interleaved, balanced treatment slots")
    if _object(suite.get("children")) != {variant: variant for variant in VARIANTS}:
        errors.append("suite.json: children must map D0-D3 to their literal relative directories")
    if not _digest(_object(suite.get("source")).get("sha256")):
        errors.append("suite.json: missing frozen source SHA256")
    if not _digest(suite.get("fixture_sha256")):
        errors.append("suite.json: missing fixture SHA256")
    workload = _object(suite.get("workload"))
    if any(
        not _digest(workload.get(name))
        for name in ("flow_sha256", "input_sha256", "model_sha256", "reference_pixel_sha256")
    ):
        errors.append("suite.json: incomplete frozen workload hashes")
    return wanted


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0 else None


def _execution_order(directory: Path, suite: dict, expected: list[dict], errors: list[str]) -> dict:
    declaration = suite.get("suite_events")
    if declaration is None:
        return {"status": "unavailable", "reason": "not_declared", "records": []}
    initial_errors = len(errors)
    if json.dumps(declaration, sort_keys=True) != json.dumps({"version": 1, "enabled": True}, sort_keys=True):
        errors.append("suite_events: unsupported declaration")
    records = []
    try:
        for line in (directory / "suite_events.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("event must be an object")
            records.append(row)
    except (OSError, ValueError) as exc:
        errors.append(f"suite_events: cannot read execution evidence ({type(exc).__name__})")
    if len(records) != len(expected):
        errors.append("suite_events: expected one event per global scheduled slot")
    previous_end = None
    for sequence, (record, slot) in enumerate(zip(records, expected, strict=False), start=1):
        if not _integer(record.get("sequence"), 1) or record["sequence"] != sequence:
            errors.append(f"suite_events {sequence}: sequence mismatch")
        if not _integer(record.get("block"), 1) or any(
            record.get(key) != slot[key] for key in ("block", "arm", "variant")
        ):
            errors.append(f"suite_events {sequence}: does not match the global schedule")
        if "outer_block_id" in record and (
            not _integer(record["outer_block_id"], 1) or record["outer_block_id"] != slot["outer_block_id"]
        ):
            errors.append(f"suite_events {sequence}: outer block mismatch")
        started, finished = _time(record.get("started_at_utc")), _time(record.get("finished_at_utc"))
        if started is None or finished is None or finished < started:
            errors.append(f"suite_events {sequence}: invalid UTC interval")
        if started and previous_end and started < previous_end:
            errors.append(f"suite_events {sequence}: execution overlaps or reverses previous slot")
        previous_end = finished
    return {"status": "measured" if len(errors) == initial_errors else "invalid", "records": records}


def _profile(manifest: dict, variant: str, fixture_hash: Any, errors: list[str]) -> None:
    warmups = 20 if variant == "D3" else 5
    enabled = variant != "D0"
    descriptor = manifest.get("profile")
    profile_id, expected = profile_identity(
        fixture_hash,
        diagnostics=enabled,
        telemetry=variant != "D2",
        warmups=warmups,
        effective_common_settings=descriptor.get("effective_common_settings") if isinstance(descriptor, dict) else None,
    )
    actual_hash = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    if descriptor != expected or actual_hash != profile_id or manifest.get("profile_id") != profile_id:
        errors.append(f"child {variant}: profile descriptor/hash does not match the designed treatment")
    if manifest.get("warmups") != warmups:
        errors.append(f"child {variant}: warmup count does not match treatment policy")
    if manifest.get("diagnostics") != diagnostics_declaration(enabled):
        errors.append(f"child {variant}: diagnostic declaration does not match treatment policy")
    if manifest.get("resource_measurement") != resource_declaration():
        errors.append(f"child {variant}: requires the four mandatory RSS checkpoints")


def _bind_child_timing(directory: Path, variant: str, execution: dict, errors: list[str]) -> None:
    """Bind runner UTC evidence to global slots, independently of worker clocks."""
    if execution["status"] == "unavailable":
        return
    initial_errors = len(errors)
    intervals = {}
    for event in execution["records"]:
        if event.get("variant") != variant:
            continue
        block, arm = event.get("block"), event.get("arm")
        started, finished = _time(event.get("started_at_utc")), _time(event.get("finished_at_utc"))
        if _integer(block, 1) and isinstance(arm, str) and arm in PAIRED_ARMS and started and finished:
            intervals[(block, arm)] = (started, finished)
    for name, start_key, end_key in (
        ("requests.jsonl", "started_at", "finished_at"),
        ("memory.jsonl", "started_at_utc", "finished_at_utc"),
    ):
        prefix = f"child {variant} suite timing {name}"
        try:
            rows = [
                json.loads(line) for line in (directory / name).read_text(encoding="utf-8").splitlines() if line.strip()
            ]
        except (OSError, ValueError) as exc:
            errors.append(f"{prefix}: cannot read evidence ({type(exc).__name__})")
            continue
        covered = set()
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                errors.append(f"{prefix} row {index}: expected an object")
                continue
            block, arm = row.get("block"), row.get("arm")
            if not _integer(block, 1) or not isinstance(arm, str) or arm not in PAIRED_ARMS:
                errors.append(f"{prefix} row {index}: invalid worker slot")
                continue
            slot = (block, arm)
            bounds = intervals.get(slot)
            started, finished = _time(row.get(start_key)), _time(row.get(end_key))
            if started is None or finished is None or finished < started:
                errors.append(f"{prefix} row {index}: missing or invalid UTC interval")
            elif bounds is None or not (bounds[0] <= started <= finished <= bounds[1]):
                errors.append(f"{prefix} row {index}: interval outside global execution slot {slot}")
            else:
                covered.add(slot)
        if covered != set(intervals):
            errors.append(f"{prefix}: missing timed evidence for global execution slots")
    if len(errors) != initial_errors:
        execution["status"] = "invalid"


def _child(
    directory: Path,
    suite: dict,
    variant: str,
    expected: list[dict],
    experiment_ids: set,
    execution: dict,
    errors: list[str],
) -> dict:
    child_errors: list[str] = []
    root = directory.resolve()
    child_dir = directory / variant
    reference = _object(suite.get("children")).get(variant)
    if reference != variant or child_dir.resolve().parent != root or not child_dir.is_dir():
        errors.append(f"child {variant}: missing or unsafe child directory")
        return {
            "directory": None,
            "valid": False,
            "overall_valid": False,
            "validation_errors": ["missing or unsafe child directory"],
            "raw_sha256": {},
        }
    raw_hashes = _hashes(child_dir, RAW_FILES)
    manifest = _read_json(child_dir / "manifest.json", child_errors, f"child {variant}/manifest.json")
    if manifest.get("kind") != "diagnostic" or manifest.get("variant") != variant:
        child_errors.append(f"child {variant}: kind or variant mismatch")
    experiment_id = manifest.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id or experiment_id in experiment_ids:
        child_errors.append(f"child {variant}: missing or duplicate experiment_id")
    else:
        experiment_ids.add(experiment_id)
    for key in ("source", "workload", "fixture_sha256"):
        if manifest.get(key) != suite.get(key):
            child_errors.append(f"child {variant}: {key} differs from frozen suite provenance")
    if (
        manifest.get("blocks") != 4
        or manifest.get("requests_per_arm") != suite.get("requests_per_arm")
        or manifest.get("mode") != suite.get("mode")
    ):
        child_errors.append(f"child {variant}: block count, sample count, or mode differs from suite")
    child_schedule = manifest.get("schedule")
    filtered = [slot for slot in expected if slot["variant"] == variant]
    if not isinstance(child_schedule, list) or [_slot(slot, variant) for slot in child_schedule] != [
        _slot(slot) for slot in filtered
    ]:
        child_errors.append(f"child {variant}: slot order does not match filtered global schedule")
    _profile(manifest, variant, suite.get("fixture_sha256"), child_errors)
    state = _read_json(child_dir / "state.json", child_errors, f"child {variant}/state.json")
    if state.get("status") != "COMPLETE":
        child_errors.append(f"child {variant}: runner state is not COMPLETE")
    _bind_child_timing(child_dir, variant, execution, child_errors)
    analysis = {}
    try:
        analysis = reporting.analyze(child_dir)
    except Exception as exc:  # Malformed child evidence must still produce an INVALID suite report.
        child_errors.append(f"child {variant}: analysis failed ({type(exc).__name__})")
    if analysis.get("overall_valid") is not True:
        child_errors.append(f"child {variant}: required latency/resource/diagnostic evidence invalid")
    for arm in PAIRED_ARMS:
        count = _object(_object(_object(analysis.get("arms")).get(arm)).get(reporting.PRIMARY)).get("n")
        if count != suite.get("requests_per_arm"):
            child_errors.append(f"child {variant}/{arm}: measured sample count does not match suite")
    if _hashes(child_dir, RAW_FILES) != raw_hashes:
        child_errors.append(f"child {variant}: raw evidence changed during analysis")
    errors.extend(child_errors)
    return {
        "directory": variant,
        "experiment_id": experiment_id,
        "profile_id": manifest.get("profile_id"),
        "effective_common_settings": _object(manifest.get("profile")).get("effective_common_settings"),
        "valid": analysis.get("valid") is True,
        "overall_valid": not child_errors,
        "validation_errors": [*child_errors, *analysis.get("validation_errors", [])],
        "resource_validity": analysis.get("resource_validity", {}),
        "diagnostic_validity": analysis.get("diagnostic_validity", {}),
        "primary_metric": analysis.get("primary_metric"),
        "arms": analysis.get("arms", {}),
        "blocks": analysis.get("blocks", {}),
        "raw_sha256": raw_hashes,
    }


def _stats(by_block: dict) -> dict:
    values = sorted(by_block.values())
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "by_block": {}}

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
        "by_block": by_block,
    }


def _comparisons(children: dict, errors: list[str]) -> dict | None:
    result = {}
    for name, (treatment, control, label) in COMPARISONS.items():
        blocks = {}
        for block in range(1, 5):
            members = {}
            for arm in PAIRED_ARMS:
                metrics = {}
                for metric in reporting.METRICS:
                    deltas = {}
                    for statistic in STATISTICS:
                        values = [
                            _object(
                                _object(_object(_object(children[variant].get("blocks")).get(str(block))).get(arm)).get(
                                    metric
                                )
                            ).get(statistic)
                            for variant in (treatment, control)
                        ]
                        if not all(_finite(value) for value in values):
                            errors.append(f"comparison {name}/{block}/{arm}: missing {metric}.{statistic}")
                        else:
                            deltas[statistic] = values[0] - values[1]
                    metrics[metric] = deltas
                members[arm] = metrics
            blocks[str(block)] = members
        arms = {
            arm: {
                metric: {
                    statistic: _stats(
                        {
                            block: members[arm][metric][statistic]
                            for block, members in blocks.items()
                            if statistic in members[arm][metric]
                        }
                    )
                    for statistic in STATISTICS
                }
                for metric in reporting.METRICS
            }
            for arm in PAIRED_ARMS
        }
        result[name] = {
            "treatment": treatment,
            "control": control,
            "policy": label,
            "blocks": blocks,
            "arms": arms,
            "unit": "ms",
        }
    return None if errors else result


def _reports(result: dict) -> tuple[str, str]:
    status = "VALID" if result["overall_valid"] else "INVALID"
    title = f"{status} | diagnostic suite ({result.get('mode', 'unavailable')})"
    lines = [f"# {title}", "", STATISTICAL_NOTE, "", "Child reports:", ""]
    links = []
    for variant in VARIANTS:
        child = result["children"][variant]
        if child.get("directory") == variant:
            lines.append(
                f"- [{variant}]({variant}/report.html): overall {'VALID' if child.get('overall_valid') else 'INVALID'}"
            )
            links.append(
                f'<li><a href="{variant}/report.html">{variant}</a>: overall {"VALID" if child.get("overall_valid") else "INVALID"}</li>'
            )
    if result["errors"]:
        lines.extend(["", "Validation errors:", "", *[f"- {error}" for error in result["errors"]]])
    tables = []
    if result["comparisons"] is not None:
        for name, comparison in result["comparisons"].items():
            label = f"{name}: {comparison['policy']}"
            lines.extend(
                [
                    "",
                    f"## {label}",
                    "",
                    "Signed differences in ms; each row compares distributions in the same outer block and arm.",
                    "",
                    "| Block | Arm | Mean | p50 | p95 | p99 |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            body = []
            for block, members in comparison["blocks"].items():
                for arm, metrics in members.items():
                    values = [
                        block,
                        arm,
                        *[f"{metrics[reporting.PRIMARY][statistic]:+.3f}" for statistic in STATISTICS],
                    ]
                    lines.append("| " + " | ".join(values) + " |")
                    body.append("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in values) + "</tr>")
            tables.append(
                f"<h2>{html.escape(label)}</h2><p>Signed differences in ms, paired by outer block and arm.</p><table><thead><tr>"
                + "".join(f"<th>{column}</th>" for column in ("Block", "Arm", "Mean", "p50", "p95", "p99"))
                + "</tr></thead><tbody>"
                + "".join(body)
                + "</tbody></table>"
            )
    errors_html = (
        "<ul>" + "".join(f"<li>{html.escape(error)}</li>" for error in result["errors"]) + "</ul>"
        if result["errors"]
        else ""
    )
    document = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>body{{font-family:system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}}"
        "table{border-collapse:collapse;margin:1rem 0}td,th{padding:.5rem;border:1px solid #bbb;text-align:right}</style></head><body>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(STATISTICAL_NOTE)}</p><h2>Child reports</h2><ul>{''.join(links)}</ul>"
        + errors_html
        + "".join(tables)
        + "</body></html>\n"
    )
    return "\n".join(lines) + "\n", document


def analyze_suite(directory: Path) -> dict:
    """Revalidate every child and write suite JSON/Markdown/HTML, leaving raw files intact."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Suite directory does not exist: {directory}")
    errors: list[str] = []
    suite_hashes = _hashes(directory, ("suite.json", "suite_events.jsonl"))
    suite = _read_json(directory / "suite.json", errors, "suite.json")
    expected = _suite_manifest(suite, errors)
    execution = _execution_order(directory, suite, expected, errors)
    experiments: set = set()
    children = {
        variant: _child(directory, suite, variant, expected, experiments, execution, errors) for variant in VARIANTS
    }
    common_profiles = [child.get("effective_common_settings") for child in children.values()]
    if any(profile is not None for profile in common_profiles) and any(
        profile != common_profiles[0] for profile in common_profiles[1:]
    ):
        errors.append("child effective common settings differ across diagnostic treatments")
    comparisons = _comparisons(children, errors) if not errors else None
    if _hashes(directory, ("suite.json", "suite_events.jsonl")) != suite_hashes:
        errors.append("suite raw evidence changed during analysis")
        comparisons = None
    result = {
        "schema_version": 1,
        "kind": "diagnostic_suite",
        "valid": not errors,
        "overall_valid": not errors,
        "errors": errors,
        "mode": suite.get("mode"),
        "requests_per_arm": suite.get("requests_per_arm"),
        "is_full_campaign": False,
        "is_full_diagnostic_suite": not errors and suite.get("mode") == "campaign",
        "source": suite.get("source"),
        "workload": suite.get("workload"),
        "fixture_sha256": suite.get("fixture_sha256"),
        "raw_sha256": suite_hashes,
        "execution_order": execution,
        "children": children,
        "comparisons": comparisons,
        "statistical_note": STATISTICAL_NOTE,
    }
    result = json.loads(json.dumps(result), parse_constant=lambda _: None)
    markdown, document = _reports(result)
    (directory / "suite_analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (directory / "suite_report.md").write_text(markdown, encoding="utf-8")
    (directory / "suite_report.html").write_text(document, encoding="utf-8")
    return result
