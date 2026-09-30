"""Offline validation and reports for the SCRFD Langflow overhead experiment.

This module neither starts a server nor performs requests. Raw files are read-only;
only derived report artifacts in the requested run directory are replaced.
"""

from __future__ import annotations

import csv
import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

ARMS = ("00", "01", "10", "11")
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
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


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


def _validate_manifest(manifest: dict, errors: list[str]) -> dict[tuple[int, str], int]:
    if manifest.get("schema_version") != 1:
        errors.append("manifest: unsupported or missing schema_version")
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
    if not _object(manifest.get("source")):
        errors.append("manifest: missing frozen source provenance")
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
    if set(expected) != {(block, arm) for block in range(1, 5) for arm in ARMS}:
        errors.append("manifest: schedule must contain every arm in every block exactly once")
    if len(schedule) != SLOT_COUNT or any(len(orders[block]) != len(ARMS) for block in range(1, 5)):
        errors.append("manifest: schedule requires 16 slots")
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
        diagnostics.append(
            {"block": slot[0], "arm": slot[1], "pid": snapshots[-1].get("pid"), "measured_attempts": attempts, **deltas}
        )
    return diagnostics


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
    status = "VALID" if result["valid"] else "INVALID"
    scope = (
        "CAMPAIGN — 1.000 request đo/arm"
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
            "## Thiết kế và cách đọc",
            "",
            "Arm có bit thứ nhất = compilation cache, bit thứ hai = warm graph registry; 1 bật, 0 tắt. "
            "HTTP connection reuse tắt ở mọi arm. Mỗi slot dùng một worker độc lập, concurrency 1. "
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
    for arm in ARMS:
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
    lines.extend(
        [
            _table(columns, [[item.get(key, "—") for key in columns] for item in result["worker_diagnostics"]]),
            "",
            "## Nguồn và khả năng tái lập",
            "",
            "manifest.json giữ thiết kế, hashes workload/source; requests.jsonl giữ mọi attempt; "
            "worker_evidence.jsonl giữ snapshot worker. Báo cáo đọc offline và không sửa ba file raw. "
            "Thiếu, sai số lượng, lỗi output/timing hoặc thiếu bằng chứng làm toàn bộ run INVALID.",
            "",
            "```json",
            json.dumps(
                {"workload": manifest.get("workload"), "source": source_summary},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


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
            output.append('<img src="charts.png" alt="Overhead: percentiles, block means and request sequence">')
        elif line.startswith("## "):
            output.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            output.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line:
            escaped = html.escape(line)
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
    scope = "1,000 measured requests/arm" if result["is_full_campaign"] else "SMOKE / trial configuration"
    title = f"VALID | {scope}" if result["valid"] else "INVALID — diagnostic only; no speedup claims"
    fig.suptitle(f"Langflow orchestration overhead\n{title}", fontsize=15, weight="bold")
    for position, arm in enumerate(ARMS):
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
    axes[0].set_xticks(range(4), [f"{arm}\nn={result['arms'][arm][PRIMARY]['n']}" for arm in ARMS])
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
    measured = [row for row in rows if row.get("phase") == "measured"]
    arms = {arm: _summarize([row for row in measured if row.get("arm") == arm]) for arm in ARMS}
    blocks = {
        str(block): {
            arm: _summarize([row for row in grouped[(block, arm)] if row.get("phase") == "measured"]) for arm in ARMS
        }
        for block in range(1, 5)
    }
    result = {
        "schema_version": 1,
        "valid": not errors,
        "validation_errors": errors,
        "is_full_campaign": manifest.get("requests_per_arm") == CAMPAIGN_REQUESTS_PER_ARM
        and manifest.get("blocks") == BLOCK_COUNT,
        "primary_metric": PRIMARY,
        "boundary": BOUNDARY,
        "manifest": manifest,
        "state": state,
        "arms": arms,
        "blocks": blocks,
        "worker_diagnostics": worker_diagnostics,
        "contrasts": _contrasts(arms) if not errors else None,
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
    return result
