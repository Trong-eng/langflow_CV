"""Create fresh mean/p95 and worker-RAM views of already verified campaigns.

This is presentation only: collection, source identity, raw evidence and the
original empirical retention criterion remain unchanged.
"""

from __future__ import annotations

import copy
import html
import json
import os
from pathlib import Path

from benchmark_analyst import revision_reporting as frozen

STATISTICS = ("mean", "p95")
PRIMARY = ("server_total_ms", "flow_api_ms")
SUMMARY_COLUMNS = ("arm", "block", "metric", "n", *STATISTICS)
COMPARE_COLUMNS = ("block", "metric", "statistic", "main", "compile", "delta_ms", "delta_percent")
MEMORY_COLUMNS = ("arm", "block", "checkpoint", "metric", "n", "attempted", "unavailable", "mean_bytes", "reasons")
CHECKPOINT_NAMES = {
    "before_warmup": "Trước warmup",
    "after_warmup": "Sau warmup",
    "after_measurement": "Sau đo",
    "after_idle": "Sau idle",
}
BOUNDARY_TEXT = (
    "server_total_ms: thời gian wall-clock trong worker, từ điểm vào ASGI bên ngoài ứng dụng đến lúc gửi xong "
    "response body. flow_api_ms: thời gian client gọi /run đến lúc đọc xong response. Upload, download ảnh, "
    "kiểm tra output, snapshot counters và thu thập RAM ở ngoài hai khoảng này. langflow_overhead_ms bằng "
    "server_total_ms trừ hợp các khoảng xử lý của bốn node SCRFD; dùng để giải thích kết quả."
)
REPLICATION_TEXT = (
    "Mỗi lượt có bốn blocks cân bằng; mỗi nhóm có 250 measured requests/block, tổng 1.000 requests/nhóm. "
    "Mỗi worker có năm warmup requests, loại khỏi steady-state. Blocks là đơn vị lặp, requests trong block "
    "là các mẫu lặp bên trong. p95 gộp chỉ mang tính mô tả; không suy ra 1.000 thí nghiệm độc lập."
)
MEMORY_TEXT = (
    "RSS/USS là bộ nhớ của toàn worker tại bốn checkpoints. RSS không phải dung lượng riêng của cache hoặc "
    "peak RAM. USS không thu được giữ null và lý do. MiB = 1.048.576 bytes."
)


def _relative(target: Path, output: Path) -> str:
    return os.path.relpath(target, output)


def _source(directory: Path) -> dict:
    analysis = frozen._read_json(directory / "analysis.json")
    receipt = frozen._read_json(directory / "report_verification.json")
    if analysis.get("status") != "VALID" or analysis.get("overall_valid") is not True:
        msg = f"Source analysis is not VALID: {directory}"
        raise ValueError(msg)
    if receipt.get("artifact_sha256", {}).get("analysis.json") != frozen._hash(directory / "analysis.json"):
        msg = f"Source analysis hash does not match its verification: {directory}"
        raise ValueError(msg)
    actual = {name: frozen._hash(directory / name) for name in frozen.RAW}
    if actual != analysis.get("raw_sha256") or actual != receipt.get("raw_sha256"):
        msg = f"Original raw integrity changed: {directory}"
        raise ValueError(msg)
    harness = analysis["manifest"]["source"]
    harness_root = Path(harness["root"])
    if any(frozen._hash(harness_root / name) != digest for name, digest in harness["files"].items()):
        msg = f"Frozen collector source changed: {directory}"
        raise ValueError(msg)
    return analysis


def _select(rows: list[dict], columns: tuple[str, ...]) -> list[dict]:
    return [{column: row.get(column) for column in columns} for row in rows]


def _memory(rows: list[dict]) -> list[dict]:
    return [
        {**row, "mean_mib": None if row["mean_bytes"] is None else row["mean_bytes"] / frozen.MIB}
        for row in _select(rows, MEMORY_COLUMNS)
    ]


def _ram_comparisons(resources: list[dict]) -> list[dict]:
    rows = []
    for checkpoint in frozen.CHECKPOINTS:
        for metric in ("rss", "uss"):
            main = frozen._matching(resources, arm="MAIN", block="all", checkpoint=checkpoint, metric=metric)
            compile_row = frozen._matching(resources, arm="COMPILE", block="all", checkpoint=checkpoint, metric=metric)
            left, right = main.get("mean_mib"), compile_row.get("mean_mib")
            rows.append(
                {
                    "checkpoint": checkpoint,
                    "metric": metric,
                    "main_mib": left,
                    "compile_mib": right,
                    "delta_mib": None if left is None or right is None else right - left,
                    "main_unavailable": main.get("unavailable"),
                    "compile_unavailable": compile_row.get("unavailable"),
                    "main_reasons": main.get("reasons"),
                    "compile_reasons": compile_row.get("reasons"),
                }
            )
    return rows


def _provenance(source: dict, directory: Path, output: Path) -> dict:
    manifest = source["manifest"]
    return {
        "raw_source_directory": str(directory),
        "original_analysis_sha256": frozen._hash(directory / "analysis.json"),
        "raw_sha256": source["raw_sha256"],
        "original_manifest": _relative(directory / "manifest.json", output),
        "groups": {
            arm: {
                "root": item["root"],
                "source": {key: item["source"].get(key) for key in ("git_revision", "sha256", "working_tree_clean")},
            }
            for arm, item in manifest["groups"].items()
        },
        "harness_sha256": manifest["source"]["sha256"],
        "workload": manifest["workload"],
        "profile_id": manifest["profile_id"],
        "fixture_sha256": manifest["fixture_sha256"],
    }


def _prepare(output: Path) -> None:
    if output.exists():
        msg = f"Presentation destination already exists: {output}"
        raise FileExistsError(msg)
    output.mkdir(parents=True)


def _link(path: str, label: str | None = None) -> str:
    return f'<a href="{html.escape(path)}">{html.escape(label or path)}</a>'


def _table(rows: list[dict], columns: tuple[str, ...]) -> str:
    return frozen._table(rows, columns)


def _document(title: str, body: str) -> str:
    return frozen._document(title, body).replace('<html lang="en">', '<html lang="vi">')


def _markdown_table(rows: list[dict], columns: tuple[str, ...]) -> list[str]:
    return [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
        *("| " + " | ".join(frozen._number(row.get(column)) for column in columns) + " |" for row in rows),
    ]


def _exports(output: Path, analysis: dict) -> dict[str, int]:
    counts = {}
    for filename, key in (
        ("summary.csv", "summary"),
        ("comparisons.csv", "comparisons"),
        ("warmup_summary.csv", "warmup"),
        ("resource_summary.csv", "resources"),
        ("ram_comparisons.csv", "ram_comparisons"),
    ):
        frozen._csv(output / filename, analysis[key])
        counts[filename] = len(analysis[key])
    return counts


def _verify(output: Path, counts: dict[str, int], sources: list[tuple[Path, dict]]) -> dict:
    frozen._json(output / "verification_receipt.json", {})
    receipt = frozen._verify_artifacts(
        output, receipt_name="verification_receipt.json", csv_counts=counts, raw_hashes={}
    )
    originals = []
    for directory, source in sources:
        if _source(directory) != source:
            msg = f"Original analysis changed while rendering: {directory}"
            raise ValueError(msg)
        current = {name: frozen._hash(directory / name) for name in frozen.RAW}
        if current != source["raw_sha256"]:
            msg = f"Original raw changed while rendering: {directory}"
            raise ValueError(msg)
        originals.append(
            {
                "directory": str(directory),
                "raw_sha256": current,
                "analysis_sha256": frozen._hash(directory / "analysis.json"),
            }
        )
    receipt.update(
        statistics=list(STATISTICS),
        original_sources=originals,
        raw_unchanged=True,
        frozen_collector_unchanged=True,
        analysis_is_presentation_only=True,
    )
    allowed = set(STATISTICS)
    analysis = frozen._read_json(output / "analysis.json")
    if any(row.get("statistic") not in allowed for row in analysis["comparisons"]):
        msg = "Comparison includes an unexpected statistic"
        raise ValueError(msg)
    for key in ("summary", "warmup", "comparisons", "resources"):
        for row in analysis[key]:
            original = sources[row.get("run", 1) - 1][1][key]
            selectors = {
                name: row[name] for name in ("arm", "block", "metric", "statistic", "checkpoint") if name in row
            }
            original_row = frozen._matching(original, **selectors)
            for field, value in row.items():
                if field in ("run", "mean_mib"):
                    continue
                if original_row.get(field) != value:
                    msg = f"Presentation value differs from original: {key}/{selectors}/{field}"
                    raise ValueError(msg)
            if (
                "mean_mib" in row
                and row["mean_bytes"] is not None
                and row["mean_mib"] * frozen.MIB != row["mean_bytes"]
            ):
                msg = "RAM MiB conversion differs from original bytes"
                raise ValueError(msg)
    receipt["metric_selection_verified"] = True
    receipt["values_equal_original"] = True
    receipt["renderer_sha256"] = frozen._hash(Path(__file__))
    frozen._json(output / "verification_receipt.json", receipt)
    return receipt


def _artifact_links(output: Path) -> str:
    names = (
        "analysis.json",
        "manifest.json",
        "summary.csv",
        "comparisons.csv",
        "warmup_summary.csv",
        "resource_summary.csv",
        "ram_comparisons.csv",
        "report.md",
        "verification_receipt.json",
    )
    return " · ".join(_link(name) for name in names)


def _raw_links(directory: Path, output: Path) -> str:
    names = (*frozen.RAW, "validation.json", "report_verification.json")
    return " · ".join(_link(_relative(directory / name, output), name) for name in names)


def render_run(original: Path | str, newdest: Path | str) -> dict:
    """Render one fresh run view from the hash-verified VALID original analysis."""
    original, output = Path(original).resolve(), Path(newdest).resolve()
    source = _source(original)
    _prepare(output)
    analysis = {
        "schema_version": 1,
        "kind": "mean_p95_presentation",
        "status": "VALID",
        "statistics": list(STATISTICS),
        "experiment_id": source["experiment_id"],
        **_provenance(source, original, output),
        "primary_metrics": list(PRIMARY),
        "explanatory_metrics": ["langflow_overhead_ms"],
        "summary": _select(source["summary"], SUMMARY_COLUMNS),
        "comparisons": [copy.deepcopy(row) for row in source["comparisons"] if row["statistic"] in STATISTICS],
        "warmup": _select(source["warmup"], (*SUMMARY_COLUMNS, "total_ms")),
        "resources": _memory(source["resources"]),
        "outcomes": source["outcomes"],
        "compilation": source["compilation"],
        "validation": source["validation"],
        "replication": REPLICATION_TEXT,
        "timing_boundaries": BOUNDARY_TEXT,
        "memory_definition": MEMORY_TEXT,
    }
    analysis["ram_comparisons"] = _ram_comparisons(analysis["resources"])
    frozen._json(output / "analysis.json", analysis)
    frozen._json(
        output / "manifest.json",
        {
            key: value
            for key, value in analysis.items()
            if key
            in (
                "kind",
                "schema_version",
                "status",
                "statistics",
                "raw_source_directory",
                "raw_sha256",
                "original_analysis_sha256",
                "original_manifest",
                "groups",
                "harness_sha256",
                "workload",
                "profile_id",
                "fixture_sha256",
            )
        },
    )
    counts = _exports(output, analysis)
    frozen._plots(output, analysis)
    title = f"MAIN và COMPILE · {source['experiment_id']} · mean, p95 và RAM"
    latency = [row for row in analysis["summary"] if row["block"] == "all" and row["metric"] in PRIMARY]
    differences = [row for row in analysis["comparisons"] if row["block"] == "all" and row["metric"] in PRIMARY]
    memory = [row for row in analysis["resources"] if row["block"] == "all"]
    body = f'<h1>{html.escape(title)}</h1><p class="badge"><strong>VALID</strong> · 1.000 measured requests/nhóm, '
    body += "output đúng; warm graph OFF; compilation cache có hits sau warmup.</p><h2>Latency thực tế</h2>"
    body += _table(latency, ("arm", "metric", "n", "mean", "p95"))
    body += '<img src="charts.png" alt="Mean và p95 server/client API latency của bốn blocks">'
    body += "<h2>Chênh lệch COMPILE−MAIN</h2><p>Số âm có lợi cho COMPILE; phần trăm dùng MAIN làm mẫu số.</p>"
    body += _table(differences, ("metric", "statistic", "main", "compile", "delta_ms", "delta_percent"))
    body += "<h2>Đánh đổi RAM: toàn worker</h2><p>" + MEMORY_TEXT + "</p>"
    body += _table(memory, ("arm", "checkpoint", "metric", "mean_mib", "n", "unavailable", "reasons"))
    body += _table(analysis["ram_comparisons"], ("checkpoint", "metric", "main_mib", "compile_mib", "delta_mib"))
    body += '<img src="tradeoffs.png" alt="Mean server latency so với RSS sau idle và RSS bốn checkpoints">'
    body += "<h2>Chi phí warmup</h2>" + _table(
        [row for row in analysis["warmup"] if row["metric"] in PRIMARY],
        ("arm", "block", "metric", "n", "total_ms", "mean", "p95"),
    )
    body += "<details><summary>Mean và p95 từng block, gồm overhead giải thích</summary>"
    body += (
        _table(analysis["summary"], SUMMARY_COLUMNS) + _table(analysis["comparisons"], COMPARE_COLUMNS) + "</details>"
    )
    body += "<h2>Ranh giới đo và đơn vị lặp</h2><p>" + BOUNDARY_TEXT + "</p><p>" + REPLICATION_TEXT + "</p>"
    body += (
        "<h2>Output, cache và danh tính nguồn</h2><p>MAIN compilation counters unavailable vì không có trong baseline; "
    )
    body += (
        "không giả lập zero.</p><pre>"
        + html.escape(
            json.dumps(
                {
                    "groups": analysis["groups"],
                    "outcomes": analysis["outcomes"],
                    "compilation": analysis["compilation"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        + "</pre>"
    )
    body += "<h2>Raw gốc và manifest đã ghim</h2><p>" + _raw_links(original, output) + "</p>"
    frozen._json(output / "verification_receipt.json", {})
    body += "<h2>CSV và xác minh</h2><p>" + _artifact_links(output) + "</p>"
    body += '<p class="muted">Chỉ tạo góc nhìn trình bày mới; raw và báo cáo gốc được giữ nguyên. '
    body += "Receipt kiểm tra links, CSV, PNG và hashes. Render trình duyệt được xác minh riêng.</p>"
    (output / "report.html").write_text(_document(title, body), encoding="utf-8")
    lines = [
        f"# {title}",
        "",
        "**VALID** — 1.000 measured requests/nhóm; warm graph OFF; output đúng; cache hits sau warmup.",
        "",
        "Latency thực tế (ms):",
        "",
        *_markdown_table(latency, ("arm", "metric", "n", "mean", "p95")),
        "",
        "COMPILE−MAIN; số âm có lợi cho COMPILE:",
        "",
        *_markdown_table(differences, ("metric", "statistic", "delta_ms", "delta_percent")),
        "",
        MEMORY_TEXT,
        "",
        *_markdown_table(memory, ("arm", "checkpoint", "metric", "mean_mib", "unavailable", "reasons")),
        "",
        "![Mean và p95 latency](charts.png)",
        "",
        "![Latency và RAM](tradeoffs.png)",
        "",
        BOUNDARY_TEXT,
        "",
        REPLICATION_TEXT,
        "",
        f"[Raw manifest đã ghim]({_relative(original / 'manifest.json', output)}) · [CSV](comparisons.csv) · "
        "[RAM CSV](resource_summary.csv) · [Verification receipt](verification_receipt.json)",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _verify(output, counts, [(original, source)])
    return analysis


def render_comparison(
    first_original: Path | str,
    repeat_original: Path | str,
    newdest: Path | str,
    *,
    original_comparison: Path | str | None = None,
) -> dict:
    """Render a fresh repeat comparison, retaining its original latency criterion."""
    first, repeat, output = Path(first_original).resolve(), Path(repeat_original).resolve(), Path(newdest).resolve()
    comparison = (
        Path(original_comparison).resolve() if original_comparison else first.parent / f"{first.name}-comparison"
    )
    sources = [_source(first), _source(repeat)]
    combined = frozen._read_json(comparison / "analysis.json")
    old_receipt = frozen._read_json(comparison / "verification_receipt.json")
    if combined.get("status") != "VALID" or old_receipt.get("artifact_sha256", {}).get("analysis.json") != frozen._hash(
        comparison / "analysis.json"
    ):
        msg = f"Original paired comparison is not verified VALID: {comparison}"
        raise ValueError(msg)
    if [run.get("raw_sha256") for run in combined["runs"]] != [source["raw_sha256"] for source in sources]:
        msg = "Original paired comparison does not reference these campaign raw hashes"
        raise ValueError(msg)
    _prepare(output)
    recommendation = copy.deepcopy(combined["recommendation"])
    recommendation["text_vi"] = (
        "Compilation cache đạt tiêu chí latency đã đặt: mean và p95 của server/client API đều giảm ở cả hai lượt. "
        "Có thể giữ có điều kiện cho workload này nếu ngân sách RAM chấp nhận được; RSS sau idle tăng và biến động "
        "giữa hai lượt, cần cân nhắc chi phí toàn worker."
        if recommendation["worth_keeping_for_latency"]
        else "Hai lượt chưa chứng minh lợi ích latency nhất quán theo tiêu chí mean và p95 đã đặt; "
        "chưa có cơ sở giữ cache chỉ để đạt mục tiêu latency này."
    )
    analyses = [
        {"run": index, "directory": str(directory), "source": _provenance(source, directory, output)}
        for index, (directory, source) in enumerate(zip((first, repeat), sources, strict=True), start=1)
    ]
    analysis = {
        "schema_version": 1,
        "kind": "mean_p95_repeat_presentation",
        "status": "VALID",
        "statistics": list(STATISTICS),
        "original_comparison_directory": str(comparison),
        "original_analysis_sha256": frozen._hash(comparison / "analysis.json"),
        "runs": analyses,
        "recommendation": recommendation,
        "consistency": copy.deepcopy(combined["consistency"]),
        "summary": [
            {"run": index, **row}
            for index, source in enumerate(sources, start=1)
            for row in _select(source["summary"], SUMMARY_COLUMNS)
        ],
        "comparisons": [copy.deepcopy(row) for row in combined["comparisons"] if row["statistic"] in STATISTICS],
        "warmup": [
            {"run": index, **row}
            for index, source in enumerate(sources, start=1)
            for row in _select(source["warmup"], (*SUMMARY_COLUMNS, "total_ms"))
        ],
        "resources": [
            {"run": index, **row}
            for index, source in enumerate(sources, start=1)
            for row in _memory(source["resources"])
        ],
        "ram_comparisons": [
            {"run": index, **row}
            for index, source in enumerate(sources, start=1)
            for row in _ram_comparisons(_memory(source["resources"]))
        ],
        "replication": REPLICATION_TEXT,
        "memory_definition": MEMORY_TEXT,
        "timing_boundaries": BOUNDARY_TEXT,
    }
    frozen._json(output / "analysis.json", analysis)
    frozen._json(
        output / "manifest.json",
        {
            key: value
            for key, value in analysis.items()
            if key
            in (
                "schema_version",
                "kind",
                "status",
                "statistics",
                "runs",
                "original_comparison_directory",
                "original_analysis_sha256",
            )
        },
    )
    counts = _exports(output, analysis)
    _comparison_plots(output, sources)
    title = "MAIN và COMPILE · Tổng hợp hai lượt · mean, p95 và RAM"
    latencies = [row for row in analysis["summary"] if row["block"] == "all" and row["metric"] in PRIMARY]
    differences = [row for row in analysis["comparisons"] if row["block"] == "all" and row["metric"] in PRIMARY]
    memories = [row for row in analysis["resources"] if row["block"] == "all"]
    body = f'<h1>{title}</h1><p class="badge"><strong>VALID</strong> · 4.000 measured requests; '
    body += html.escape(recommendation["text_vi"]) + "</p><h2>Mean và p95 latency thực tế</h2>"
    body += _table(latencies, ("run", "arm", "metric", "mean", "p95"))
    body += _table(differences, ("run", "metric", "statistic", "delta_ms", "delta_percent"))
    body += '<img src="charts.png" alt="Chênh lệch mean và p95 server/client API của hai lượt">'
    body += "<h2>Đánh đổi RAM và độ nhất quán repeat</h2><p>" + MEMORY_TEXT + "</p>"
    body += _table(memories, ("run", "arm", "checkpoint", "metric", "mean_mib", "unavailable", "reasons"))
    body += _table(analysis["ram_comparisons"], ("run", "checkpoint", "metric", "main_mib", "compile_mib", "delta_mib"))
    body += '<img src="tradeoffs.png" alt="Chênh lệch RSS COMPILE−MAIN tại bốn checkpoints của hai lượt">'
    body += (
        "<p>RSS COMPILE−MAIN sau idle (MiB): "
        + ", ".join(f"{value:+.2f}" for value in recommendation["rss_delta_after_idle_mib"])
        + ".</p>"
    )
    body += (
        "<p>RSS COMPILE−MAIN sau đo (MiB): "
        + ", ".join(f"{value:+.2f}" for value in recommendation["rss_delta_after_measurement_mib"])
        + ".</p>"
    )
    body += "<p>Chưa đặt mức latency tuyệt đối hay ngân sách RAM chấp nhận được. Kết luận chỉ áp dụng workload và cấu hình đã ghim; "
    body += "không suy rộng thành SLA hoặc lợi ích cho mọi flow. Tiêu chí giữ cache không đổi: mean và p95 của cả hai latency thực tế giảm ở cả hai lượt.</p>"
    body += "<h2>Từng block và chi phí warmup</h2>"
    body += _table(analysis["comparisons"], ("run", *COMPARE_COLUMNS))
    body += _table(
        [row for row in analysis["warmup"] if row["metric"] in PRIMARY],
        ("run", "arm", "block", "metric", "n", "total_ms", "mean", "p95"),
    )
    body += "<h2>Ranh giới đo và đơn vị lặp</h2><p>" + BOUNDARY_TEXT + "</p><p>" + REPLICATION_TEXT + "</p>"
    body += "<h2>Hai lượt và raw gốc</h2>"
    for index, directory in enumerate((first, repeat), start=1):
        new_report = directory.parent / f"{directory.name}-mean-p95" / "report.html"
        body += f"<p>Lượt {index}: " + _link(_relative(new_report, output), "Báo cáo mean và p95") + "</p>"
        body += "<p>" + _raw_links(directory, output) + "</p>"
    frozen._json(output / "verification_receipt.json", {})
    body += "<h2>CSV và xác minh</h2><p>" + _artifact_links(output) + "</p>"
    body += '<p class="muted">Raw và báo cáo gốc được giữ nguyên; render trình duyệt được xác minh riêng.</p>'
    (output / "report.html").write_text(_document(title, body), encoding="utf-8")
    lines = [
        f"# {title}",
        "",
        "**VALID** — " + recommendation["text_vi"],
        "",
        "Latency thực tế (ms):",
        "",
        *_markdown_table(latencies, ("run", "arm", "metric", "mean", "p95")),
        "",
        "COMPILE−MAIN:",
        "",
        *_markdown_table(differences, ("run", "metric", "statistic", "delta_ms", "delta_percent")),
        "",
        MEMORY_TEXT,
        "",
        *_markdown_table(memories, ("run", "arm", "checkpoint", "metric", "mean_mib", "unavailable", "reasons")),
        "",
        "![Mean và p95 latency](charts.png)",
        "",
        "![RAM bốn checkpoints](tradeoffs.png)",
        "",
        REPLICATION_TEXT,
        "",
        "[Manifest](manifest.json) · [Latency CSV](comparisons.csv) · [RAM CSV](resource_summary.csv) · "
        "[Verification receipt](verification_receipt.json)",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _verify(output, counts, [(first, sources[0]), (repeat, sources[1])])
    return analysis


def _comparison_plots(output: Path, sources: list[dict]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
    for index, metric in enumerate(PRIMARY):
        for run, source in enumerate(sources, start=1):
            values = [
                frozen._matching(source["comparisons"], block="all", metric=metric, statistic=statistic)[
                    "delta_percent"
                ]
                for statistic in STATISTICS
            ]
            axes[index].bar(
                [position + (run - 1.5) * 0.35 for position in range(2)], values, width=0.35, label=f"Lượt {run}"
            )
        axes[index].set(title=metric, ylabel="COMPILE−MAIN (%)", xticks=range(2), xticklabels=STATISTICS)
        axes[index].axhline(0, color="#334455", lw=0.8)
        axes[index].legend()
    figure.suptitle("Mean và p95 latency thực tế · số âm có lợi cho COMPILE")
    figure.savefig(output / "charts.png", dpi=140)
    plt.close(figure)
    figure, axis = plt.subplots(figsize=(12, 5), layout="constrained")
    for run, source in enumerate(sources, start=1):
        rows = _ram_comparisons(_memory(source["resources"]))
        values = [
            frozen._matching(rows, checkpoint=checkpoint, metric="rss")["delta_mib"]
            for checkpoint in frozen.CHECKPOINTS
        ]
        axis.plot(range(4), values, marker="o", label=f"Lượt {run}")
    axis.set(
        title="RSS toàn worker: COMPILE−MAIN",
        ylabel="MiB",
        xticks=range(4),
        xticklabels=[CHECKPOINT_NAMES[name] for name in frozen.CHECKPOINTS],
    )
    axis.axhline(0, color="#334455", lw=0.8)
    axis.grid(alpha=0.2)
    axis.legend()
    figure.savefig(output / "tradeoffs.png", dpi=140)
    plt.close(figure)
