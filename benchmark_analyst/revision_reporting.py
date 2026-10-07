"""Offline, block-aware MAIN versus compilation-cache reporting.

Raw evidence is immutable. Each report destination is created once; reports never
exclude an adverse outcome to obtain a valid campaign or a positive conclusion.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any
from urllib.parse import unquote

ARMS = ("MAIN", "COMPILE")
METRICS = ("server_total_ms", "flow_api_ms", "langflow_overhead_ms")
STATISTICS = ("mean", "p50", "p95", "p99")
CHECKPOINTS = ("before_warmup", "after_warmup", "after_measurement", "after_idle")
RAW = ("manifest.json", "requests.jsonl", "worker_evidence.jsonl", "memory.jsonl", "state.json")
ARTIFACTS = (
    "analysis.json",
    "report.html",
    "report.md",
    "summary.csv",
    "comparisons.csv",
    "warmup_summary.csv",
    "resource_summary.csv",
    "charts.png",
    "tradeoffs.png",
    "report_verification.json",
)
MIB = 1024 * 1024
BOUNDARIES = {
    "server_total_ms": (
        "Worker wall-clock from entry into the benchmark middleware for /run until the response body is sent. "
        "Includes graph/component construction, validation, authentication within this boundary, model processing "
        "and response serialization. It is not CPU time."
    ),
    "flow_api_ms": (
        "Client wall-clock around the /run HTTP call through reading its response. Upload, output image download, "
        "output validation, counter snapshots and memory collection occur outside this interval."
    ),
    "langflow_overhead_ms": (
        "server_total_ms minus the union of the four SCRFD output-method intervals; explanatory metric only. "
        "It does not replace actual server or client API latency."
    ),
    "memory": (
        "RSS and optional USS describe the whole worker at four checkpoints. RSS is neither cache-only memory "
        "nor peak RAM. USS unavailable remains null with its recorded reason."
    ),
}


def _finite(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _statistics(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, **dict.fromkeys(STATISTICS)}

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        left, right = math.floor(position), math.ceil(position)
        return ordered[left] + (ordered[right] - ordered[left]) * (position - left)

    return {
        "n": len(ordered),
        "mean": fmean(ordered),
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
    }


def _successful(row: dict) -> bool:
    return row.get("outcome") == "success" and row.get("output_valid") is True


def _delta(main: Any, compile_value: Any) -> dict:
    if main is None or compile_value is None:
        return {"delta_ms": None, "delta_percent": None, "percent_reason": "missing_value"}
    difference = compile_value - main
    return {
        "delta_ms": difference,
        "delta_percent": None if main == 0 else 100 * difference / main,
        "percent_reason": "zero_baseline" if main == 0 else None,
    }


def _matching(rows: list[dict], **selectors: Any) -> dict:
    return next((row for row in rows if all(row.get(key) == value for key, value in selectors.items())), {})


def build_analysis(
    manifest: dict, requests: list[dict], evidence: list[dict], memory: list[dict], validation: dict
) -> dict:
    """Compute descriptive metrics while preserving the authoritative validity gate."""
    summaries, warmups, resources, outcomes = [], [], [], {}
    for arm in ARMS:
        outcomes[arm] = {}
        for phase in ("warmup", "measured"):
            attempts = [row for row in requests if row.get("arm") == arm and row.get("phase") == phase]
            successes = sum(_successful(row) for row in attempts)
            outcomes[arm][phase] = {
                "attempted": len(attempts),
                "successful": successes,
                "failed": len(attempts) - successes,
                "counts": dict(Counter(str(row.get("outcome", "missing")) for row in attempts)),
            }
        for block in (*range(1, 5), "all"):
            selected = [
                row for row in requests if row.get("arm") == arm and (block == "all" or row.get("block") == block)
            ]
            for phase, destination in (("measured", summaries), ("warmup", warmups)):
                successful = [row for row in selected if row.get("phase") == phase and _successful(row)]
                for metric in METRICS:
                    values = [float(row[metric]) for row in successful if _finite(row.get(metric))]
                    summary = {"arm": arm, "block": block, "metric": metric, **_statistics(values)}
                    if phase == "warmup":
                        summary["total_ms"] = sum(values) if values else None
                    destination.append(summary)
            for checkpoint in CHECKPOINTS:
                checkpoints = [
                    row
                    for row in memory
                    if row.get("arm") == arm
                    and row.get("checkpoint") == checkpoint
                    and (block == "all" or row.get("block") == block)
                ]
                for metric in ("rss", "uss"):
                    samples = [row.get(metric, {}) for row in checkpoints]
                    values = [
                        float(sample["bytes"])
                        for sample in samples
                        if sample.get("status") == "measured" and _finite(sample.get("bytes"))
                    ]
                    reasons = Counter(
                        str(sample.get("reason") or "missing_reason")
                        for sample in samples
                        if sample.get("status") != "measured" or not _finite(sample.get("bytes"))
                    )
                    resources.append(
                        {
                            "arm": arm,
                            "block": block,
                            "checkpoint": checkpoint,
                            "metric": metric,
                            "n": len(values),
                            "attempted": len(samples),
                            "unavailable": len(samples) - len(values),
                            "mean_bytes": fmean(values) if values else None,
                            "min_bytes": min(values) if values else None,
                            "max_bytes": max(values) if values else None,
                            "reasons": dict(reasons),
                        }
                    )
    comparisons = []
    for block in (*range(1, 5), "all"):
        for metric in METRICS:
            main = _matching(summaries, arm="MAIN", block=block, metric=metric)
            compile_summary = _matching(summaries, arm="COMPILE", block=block, metric=metric)
            for statistic in STATISTICS:
                left, right = main.get(statistic), compile_summary.get(statistic)
                comparisons.append(
                    {
                        "block": block,
                        "metric": metric,
                        "statistic": statistic,
                        "main": left,
                        "compile": right,
                        **_delta(left, right),
                    }
                )
    valid = validation.get("overall_valid", validation.get("valid")) is True
    compilation = {
        "MAIN": {"status": "unavailable", "reason": "absent_in_baseline", "counters": None},
        "COMPILE": {
            "status": "available",
            "blocks": [
                {
                    "block": row.get("block"),
                    "delta": row.get("compilation_delta"),
                    "after_warmup": row.get("after_warmup", {}).get("compilation"),
                    "after_measurement": row.get("after_measurement", {}).get("compilation"),
                }
                for row in evidence
                if row.get("arm") == "COMPILE"
            ],
        },
        "validated_evidence": validation.get("compilation_evidence"),
    }
    return {
        "schema_version": 1,
        "kind": "revision_comparison_report",
        "status": "VALID" if valid else "INVALID",
        "overall_valid": valid,
        "experiment_id": manifest.get("experiment_id"),
        "mode": manifest.get("mode"),
        "primary_metrics": list(METRICS[:2]),
        "explanatory_metrics": [METRICS[2]],
        "manifest": manifest,
        "validation": validation,
        "raw_sha256": validation.get("raw_sha256", {}),
        "boundaries": BOUNDARIES,
        "summary": summaries,
        "comparisons": comparisons,
        "warmup": warmups,
        "resources": resources,
        "outcomes": outcomes,
        "compilation": compilation,
        "replication": {
            "unit": "balanced block",
            "units_per_campaign": 4,
            "description": (
                "Four blocks are the replication units. The 1,000 measured requests per group are repeated samples "
                "within those blocks, not 1,000 independent experiments. Pooled percentiles are descriptive; "
                "no request-level confidence interval, significance test or speedup probability is claimed."
            ),
        },
        "recommendation": {
            "supported": valid and manifest.get("mode") == "campaign",
            "text": (
                "Await the repeat campaign before recommending retention for latency."
                if valid
                else "INVALID evidence: no latency improvement or retention conclusion is supported."
            ),
        },
    }


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_lines(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # The validator reports corruption; this remains INVALID diagnostic evidence.
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(
            {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            }
            for row in rows
        )


def _number(value: Any) -> str:
    if value is None:
        return "unavailable"
    return f"{value:.3f}" if isinstance(value, (int, float)) else str(value)


def _table(rows: list[dict], columns: tuple[str, ...]) -> str:
    headings = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    cells = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_number(row.get(column)))}</td>" for column in columns) + "</tr>"
        for row in rows
    )
    return f'<div class="scroll"><table><thead><tr>{headings}</tr></thead><tbody>{cells}</tbody></table></div>'


def _document(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>"
        "body{font:16px/1.6 system-ui,sans-serif;background:#f2f5f8;color:#182637;margin:0}"
        "main{max-width:1180px;margin:28px auto;padding:30px;background:white;border-radius:14px}"
        "h1{font-size:32px;line-height:1.25}h2{margin-top:32px;color:#213e58}"
        "a{color:#0065a8}table{border-collapse:collapse;width:100%;font-size:14px}"
        "th,td{padding:8px 12px;border-bottom:1px solid #dbe3ec;text-align:left;white-space:nowrap}"
        "th{background:#edf3f8}.scroll{overflow:auto}img{width:100%;height:auto}"
        "pre{background:#edf3f8;padding:16px;overflow:auto;font-size:12px}"
        ".badge{background:#edf3f8;border-left:6px solid #136c84;padding:14px}"
        ".invalid{border-color:#bd3c37;background:#fff1ef}.muted{color:#506578}"
        "</style></head><body><main>" + body + "</main></body></html>"
    )


def _plots(directory: Path, analysis: dict) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"MAIN": "#34556d", "COMPILE": "#d25b33"}
    figure, axes = plt.subplots(2, 2, figsize=(12, 7), layout="constrained")
    for index, metric in enumerate(METRICS[:2]):
        for column, statistic in enumerate(("mean", "p95")):
            axis = axes[index, column]
            for arm in ARMS:
                values = [
                    _matching(analysis["summary"], arm=arm, block=block, metric=metric).get(statistic)
                    for block in range(1, 5)
                ]
                axis.plot(
                    range(1, 5),
                    [math.nan if value is None else value for value in values],
                    marker="o",
                    label=arm,
                    color=colors[arm],
                )
            axis.set(title=f"{metric}: {statistic}", xlabel="Balanced block", ylabel="Milliseconds", xticks=range(1, 5))
            axis.grid(alpha=0.2)
            axis.legend()
    figure.suptitle(f"Actual measured latency · {analysis['status']} · four block units")
    figure.savefig(directory / "charts.png", dpi=140)
    plt.close(figure)
    figure, axes = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
    for arm in ARMS:
        for block in range(1, 5):
            rss = _matching(analysis["resources"], arm=arm, block=block, checkpoint="after_idle", metric="rss").get(
                "mean_bytes"
            )
            latency = _matching(analysis["summary"], arm=arm, block=block, metric="server_total_ms").get("mean")
            if rss is not None and latency is not None:
                axes[0].scatter(rss / MIB, latency, color=colors[arm], label=arm if block == 1 else None)
                axes[0].annotate(str(block), (rss / MIB, latency), xytext=(4, 4), textcoords="offset points")
        values = [
            _matching(analysis["resources"], arm=arm, block="all", checkpoint=checkpoint, metric="rss").get(
                "mean_bytes"
            )
            for checkpoint in CHECKPOINTS
        ]
        axes[1].plot(
            range(4),
            [math.nan if value is None else value / MIB for value in values],
            marker="o",
            color=colors[arm],
            label=arm,
        )
    axes[0].set(
        title="Latency versus retained worker RSS",
        xlabel="After-idle whole-worker RSS (MiB)",
        ylabel="Block mean server latency (ms)",
    )
    axes[1].set(
        title="Whole-worker RSS at checkpoints",
        ylabel="Mean across four workers (MiB)",
        xticks=range(4),
        xticklabels=["Before warmup", "After warmup", "After measure", "After idle"],
    )
    for axis in axes:
        axis.grid(alpha=0.2)
        axis.legend()
    figure.suptitle(f"Observed latency–RAM trade-off · {analysis['status']} · not peak or cache-only RAM")
    figure.savefig(directory / "tradeoffs.png", dpi=140)
    plt.close(figure)


def _run_documents(directory: Path, analysis: dict) -> None:
    title = f"MAIN versus COMPILE · {analysis['experiment_id']}"
    status = analysis["status"]
    main_rows = [row for row in analysis["summary"] if row["block"] == "all" and row["metric"] in METRICS[:2]]
    overall = [row for row in analysis["comparisons"] if row["block"] == "all"]
    raw_names = (*RAW, "validation.json") if (directory / "validation.json").is_file() else RAW
    raw_links = " · ".join(f'<a href="{name}">{name}</a>' for name in raw_names)
    artifact_links = " · ".join(
        f'<a href="{name}">{name}</a>'
        for name in ARTIFACTS
        if name not in ("report.html", "charts.png", "tradeoffs.png")
    )
    body = (
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="badge {"invalid" if status == "INVALID" else ""}"><strong>{status}</strong> · '
        f"{html.escape(analysis['recommendation']['text'])}</p>"
        "<h2>Actual measured latency</h2><p>All figures in milliseconds; steady-state excludes five warmup requests per worker. "
        "Only successful requests with valid output enter descriptive latency summaries. Every failure is retained and invalidates "
        "the campaign through the validator.</p>"
        + _table(main_rows, ("arm", "metric", "n", *STATISTICS))
        + '<img src="charts.png" alt="Mean and p95 server and client API latency for each of the four blocks">'
        + "<h2>COMPILE minus MAIN</h2><p>Negative latency differences favor COMPILE. Percentage denominator is MAIN. "
        "Null percentages remain unavailable when the baseline is zero.</p>"
        + _table(overall, ("metric", "statistic", "main", "compile", "delta_ms", "delta_percent"))
        + "<details><summary>Every block and percentile</summary>"
        + _table(
            analysis["comparisons"], ("block", "metric", "statistic", "main", "compile", "delta_ms", "delta_percent")
        )
        + "</details><h2>Warmup cost</h2><p>Total measured request time excludes process startup and instrumentation probes.</p>"
        + _table(
            [row for row in analysis["warmup"] if row["metric"] in METRICS[:2]],
            ("arm", "block", "metric", "n", "total_ms", "mean", "p95"),
        )
        + "<h2>Whole-worker memory</h2><p>"
        + html.escape(BOUNDARIES["memory"])
        + "</p>"
        + '<img src="tradeoffs.png" alt="Latency versus whole-worker RSS after idle and RSS at all four checkpoints">'
        + _table(
            [row for row in analysis["resources"] if row["block"] == "all"],
            ("arm", "checkpoint", "metric", "n", "unavailable", "mean_bytes", "reasons"),
        )
        + "<details><summary>All worker checkpoints</summary>"
        + _table(
            analysis["resources"], ("arm", "block", "checkpoint", "metric", "mean_bytes", "unavailable", "reasons")
        )
        + "</details><h2>Replication and timing boundaries</h2><p>"
        + html.escape(analysis["replication"]["description"])
        + "</p>"
        + "".join(
            f"<p><strong>{html.escape(metric)}</strong>: {html.escape(boundary)}</p>"
            for metric, boundary in BOUNDARIES.items()
        )
        + "<h2>Validity, output outcomes and cache behavior</h2><p>Warm graph is OFF in both groups. MAIN compilation counters "
        "are unavailable in the original baseline; they are never represented as fabricated zero counters.</p><pre>"
        + html.escape(
            json.dumps(
                {
                    "validation": analysis["validation"],
                    "outcomes": analysis["outcomes"],
                    "compilation": analysis["compilation"],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        + "</pre><h2>Frozen source and workload</h2><pre>"
        + html.escape(json.dumps(analysis["manifest"], indent=2, ensure_ascii=False))
        + f"</pre><h2>Raw evidence</h2><p>{raw_links}</p><h2>Exports and verification</h2><p>{artifact_links}</p>"
        + '<p class="muted">Verification checks file links, CSV parseability, PNG structure and raw hashes. '
        "Browser visual rendering is a separate check and is not claimed here.</p>"
    )
    (directory / "report.html").write_text(_document(title, body), encoding="utf-8")
    lines = [
        f"# {title}",
        "",
        f"**{status}** — {analysis['recommendation']['text']}",
        "",
        analysis["replication"]["description"],
        "",
        "Actual latency (pooled descriptive samples; milliseconds):",
        "",
        "| Group | Metric | n | Mean | p50 | p95 | p99 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    lines.extend(
        "| " + " | ".join(_number(row.get(column)) for column in ("arm", "metric", "n", *STATISTICS)) + " |"
        for row in main_rows
    )
    lines.extend(
        [
            "",
            "COMPILE−MAIN; negative latency favors COMPILE:",
            "",
            "| Metric | Statistic | Delta ms | Delta % |",
            "|---|---|---:|---:|",
        ]
    )
    lines.extend(
        "| "
        + " | ".join(_number(row.get(column)) for column in ("metric", "statistic", "delta_ms", "delta_percent"))
        + " |"
        for row in overall
    )
    lines.extend(
        [
            "",
            BOUNDARIES["memory"],
            "",
            "![Actual measured latency](charts.png)",
            "",
            "![Latency–RAM trade-off](tradeoffs.png)",
            "",
        ]
    )
    lines.extend(f"- **{metric}**: {boundary}" for metric, boundary in BOUNDARIES.items())
    lines.extend(
        [
            "",
            "MAIN compilation counters: unavailable, absent in baseline. Warm graph OFF in both groups.",
            "",
            "Output outcomes: " + json.dumps(analysis["outcomes"], ensure_ascii=False),
            "",
            "Validation errors: " + json.dumps(analysis["validation"].get("errors", []), ensure_ascii=False),
            "",
            "Files: " + " · ".join(f"[{name}]({name})" for name in (*raw_names, *ARTIFACTS) if name != "report.md"),
            "",
            "Structural artifact verification only; browser visual rendering is not claimed.",
        ]
    )
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _verify_artifacts(
    directory: Path, *, receipt_name: str, csv_counts: dict[str, int], raw_hashes: dict[str, str]
) -> dict:
    from PIL import Image

    missing = []
    for filename in ("report.html", "report.md"):
        text = (directory / filename).read_text(encoding="utf-8")
        links = (
            re.findall(r'(?:href|src)="([^"]+)"', text)
            if filename.endswith(".html")
            else re.findall(r"\]\(([^)]+)\)", text)
        )
        missing.extend(link for link in links if not (directory / unquote(link)).is_file())
    dimensions, csv_rows = {}, {}
    for path in directory.glob("*.png"):
        with Image.open(path) as picture:
            dimensions[path.name] = list(picture.size)
            picture.verify()
    for name, expected in csv_counts.items():
        with (directory / name).open(encoding="utf-8", newline="") as handle:
            csv_rows[name] = sum(1 for _ in csv.DictReader(handle))
        if csv_rows[name] != expected:
            msg = f"CSV row count mismatch: {name}"
            raise ValueError(msg)
    raw_current = {name: _hash(directory / name) for name in raw_hashes if (directory / name).is_file()}
    receipt = {
        "schema_version": 1,
        "links_valid": not missing,
        "missing_links": missing,
        "raw_unchanged": raw_current == raw_hashes,
        "raw_sha256": raw_current,
        "csv_rows": csv_rows,
        "png_dimensions": dimensions,
        "visual_rendering_verified": False,
        "artifact_sha256": {
            path.name: _hash(path)
            for path in directory.iterdir()
            if path.is_file() and path.name not in (*RAW, receipt_name)
        },
    }
    _json(directory / receipt_name, receipt)
    if missing or raw_current != raw_hashes:
        msg = "Report link or raw integrity verification failed"
        raise ValueError(msg)
    return receipt


def _validate(directory: Path) -> dict:
    from benchmark_analyst.revision_campaign import validate_run

    return validate_run(directory)


def report_run(directory: Path | str) -> dict:
    """Validate immutable raw evidence and create one set of report artifacts."""
    directory = Path(directory).resolve()
    for name in ARTIFACTS:
        if (directory / name).exists():
            msg = f"Report destination already exists: {directory / name}"
            raise FileExistsError(msg)
    validation = _validate(directory)
    raw_hashes = {name: _hash(directory / name) for name in RAW if (directory / name).is_file()}
    analysis = build_analysis(
        _read_json(directory / "manifest.json"),
        _read_lines(directory / "requests.jsonl"),
        _read_lines(directory / "worker_evidence.jsonl"),
        _read_lines(directory / "memory.jsonl"),
        validation,
    )
    analysis["raw_sha256"] = raw_hashes
    _json(directory / "analysis.json", analysis)
    counts = {}
    for filename, key in (
        ("summary.csv", "summary"),
        ("comparisons.csv", "comparisons"),
        ("warmup_summary.csv", "warmup"),
        ("resource_summary.csv", "resources"),
    ):
        _csv(directory / filename, analysis[key])
        counts[filename] = len(analysis[key])
    _plots(directory, analysis)
    _json(directory / "report_verification.json", {})
    _run_documents(directory, analysis)
    _verify_artifacts(directory, receipt_name="report_verification.json", csv_counts=counts, raw_hashes=raw_hashes)
    return analysis


def _run_delta(analysis: dict, metric: str, statistic: str = "mean") -> dict:
    return _matching(analysis["comparisons"], block="all", metric=metric, statistic=statistic)


def _ram_delta(analysis: dict, checkpoint: str) -> float | None:
    main = _matching(analysis["resources"], arm="MAIN", block="all", checkpoint=checkpoint, metric="rss").get(
        "mean_bytes"
    )
    compile_rss = _matching(analysis["resources"], arm="COMPILE", block="all", checkpoint=checkpoint, metric="rss").get(
        "mean_bytes"
    )
    return None if main is None or compile_rss is None else (compile_rss - main) / MIB


def compare_campaigns(first: Path | str, repeat: Path | str, output: Path | str) -> dict:
    """Compare two complete campaigns without treating requests as replicates."""
    directories, output = [Path(first).resolve(), Path(repeat).resolve()], Path(output).resolve()
    if output.exists():
        msg = f"Comparison destination already exists: {output}"
        raise FileExistsError(msg)
    analyses, errors, runs, worker_generations = [], [], [], []
    if directories[0] == directories[1]:
        errors.append("repeat must be a distinct campaign directory")
    for directory in directories:
        saved_analysis = _read_json(directory / "analysis.json")
        if not saved_analysis:
            msg = f"Missing analysis: {directory}"
            raise ValueError(msg)
        validation = _validate(directory)
        memory = _read_lines(directory / "memory.jsonl")
        analysis = build_analysis(
            _read_json(directory / "manifest.json"),
            _read_lines(directory / "requests.jsonl"),
            _read_lines(directory / "worker_evidence.jsonl"),
            memory,
            validation,
        )
        current = {name: _hash(directory / name) for name in RAW if (directory / name).is_file()}
        if (
            validation.get("overall_valid", validation.get("valid")) is not True
            or saved_analysis.get("status") != "VALID"
        ):
            errors.append(f"{directory.name}: invalid campaign")
        if saved_analysis.get("raw_sha256") != current:
            errors.append(f"{directory.name}: raw evidence changed after analysis")
        for key in ("summary", "comparisons", "warmup", "resources", "outcomes", "compilation"):
            if saved_analysis.get(key) != analysis.get(key):
                errors.append(f"{directory.name}: saved {key} differs from recomputed raw evidence")
        receipt = _read_json(directory / "report_verification.json")
        if receipt.get("artifact_sha256", {}).get("analysis.json") != _hash(directory / "analysis.json"):
            errors.append(f"{directory.name}: saved analysis hash differs from report verification")
        if analysis.get("mode") != "campaign":
            errors.append(f"{directory.name}: smoke cannot substitute for a full campaign")
        identities = {
            (row.get("pid"), row.get("process_create_time"))
            for row in memory
            if row.get("pid") is not None and row.get("process_create_time") is not None
        }
        if not identities:
            errors.append(f"{directory.name}: missing repeat worker generations")
        worker_generations.append(identities)
        analyses.append(analysis)
        runs.append(
            {
                "directory": str(directory),
                "report": os.path.relpath(directory / "report.html", output),
                "manifest": os.path.relpath(directory / "manifest.json", output),
                "raw_sha256": current,
                "analysis_sha256": _hash(directory / "analysis.json"),
                "validation": validation,
                "outcomes": analysis["outcomes"],
                "source_groups": analysis["manifest"].get("groups"),
            }
        )
    if analyses[0].get("experiment_id") == analyses[1].get("experiment_id"):
        errors.append("repeat reused the first experiment identity")
    if worker_generations[0] & worker_generations[1]:
        errors.append("repeat reused a worker process generation")
    for key in (
        "groups",
        "source",
        "workload",
        "fixture_sha256",
        "profile_id",
        "profile",
        "resource_measurement",
        "runtime",
        "schedule",
        "warmups",
        "requests_per_arm",
        "per_block",
        "blocks",
    ):
        if analyses[0]["manifest"].get(key) != analyses[1]["manifest"].get(key):
            errors.append(f"repeat provenance mismatch: {key}")
    consistency, combined_rows = {}, []
    for metric in METRICS:
        deltas = [_run_delta(analysis, metric).get("delta_ms") for analysis in analyses]
        directions = [None if value is None else (1 if value > 0 else -1 if value < 0 else 0) for value in deltas]
        block_deltas = [
            [
                _matching(analysis["comparisons"], block=block, metric=metric, statistic="mean").get("delta_ms")
                for block in range(1, 5)
            ]
            for analysis in analyses
        ]
        consistency[metric] = {
            "mean_delta_ms": deltas,
            "direction_consistent": None not in directions and directions[0] == directions[1],
            "block_mean_delta_ms": block_deltas,
            "favorable_blocks": [sum(value is not None and value < 0 for value in values) for values in block_deltas],
        }
        for run_index, analysis in enumerate(analyses, start=1):
            combined_rows.extend(
                {"run": run_index, **row} for row in analysis["comparisons"] if row["metric"] == metric
            )
    worth = not errors and all(
        all(
            _run_delta(analysis, metric, statistic).get("delta_ms") is not None
            and _run_delta(analysis, metric, statistic)["delta_ms"] < 0
            for analysis in analyses
            for statistic in ("mean", "p95")
        )
        for metric in METRICS[:2]
    )
    recommendation = {
        "supported": not errors,
        "worth_keeping_for_latency": worth if not errors else None,
        "criterion": "Observed mean and p95 of both actual latency metrics improve in both full campaigns.",
        "rss_delta_after_idle_mib": [_ram_delta(analysis, "after_idle") for analysis in analyses],
        "rss_delta_after_measurement_mib": [_ram_delta(analysis, "after_measurement") for analysis in analyses],
        "text": (
            "INVALID evidence: no latency improvement or retention recommendation is supported."
            if errors
            else "Compilation cache meets the observed latency criterion in both runs; consider retention against the measured whole-worker RAM cost."
            if worth
            else "The two runs do not establish a consistent mean and p95 benefit for actual server and client API latency; keeping the cache solely for this latency goal is not supported."
        ),
        "limitations": (
            "This empirical decision is specific to the frozen workload and four blocks per campaign. "
            "No absolute latency target or acceptable RAM budget was specified; no SLA or universal speedup is inferred."
        ),
    }
    result = {
        "schema_version": 1,
        "kind": "revision_comparison_repeat",
        "status": "INVALID" if errors else "VALID",
        "errors": errors,
        "runs": runs,
        "consistency": consistency,
        "recommendation": recommendation,
        "comparisons": combined_rows,
        "replication": analyses[0]["replication"],
        "boundaries": BOUNDARIES,
    }
    output.mkdir(parents=True)
    _json(output / "analysis.json", result)
    _csv(output / "comparisons.csv", combined_rows)
    links = " · ".join(
        f'<a href="{html.escape(run["report"])}">Run {index}</a> '
        f'(<a href="{html.escape(run["manifest"])}">manifest</a>)'
        for index, run in enumerate(runs, start=1)
    )
    table_rows = [
        {
            "metric": metric,
            "run1_delta_ms": detail["mean_delta_ms"][0],
            "run2_delta_ms": detail["mean_delta_ms"][1],
            "consistent_direction": detail["direction_consistent"],
            "favorable_blocks": str(detail["favorable_blocks"]),
        }
        for metric, detail in consistency.items()
    ]
    body = f'<h1>MAIN versus COMPILE: campaign and repeat</h1><p class="badge {"invalid" if errors else ""}"><strong>{result["status"]}</strong> · '
    body += html.escape(recommendation["text"]) + f"</p><p>{links}</p><h2>Repeat consistency</h2>"
    body += _table(table_rows, ("metric", "run1_delta_ms", "run2_delta_ms", "consistent_direction", "favorable_blocks"))
    body += (
        "<p>"
        + html.escape(result["replication"]["description"])
        + "</p><h2>Latency and whole-worker RAM trade-off</h2><pre>"
    )
    body += (
        html.escape(json.dumps(recommendation, indent=2, ensure_ascii=False))
        + "</pre><h2>Every observed block and percentile</h2>"
    )
    body += _table(
        combined_rows, ("run", "block", "metric", "statistic", "main", "compile", "delta_ms", "delta_percent")
    )
    body += (
        "<h2>Cross-run provenance and output checks</h2><pre>"
        + html.escape(json.dumps(runs, indent=2, ensure_ascii=False))
        + "</pre>"
    )
    body += '<h2>Verification</h2><p><a href="analysis.json">analysis.json</a> · <a href="comparisons.csv">comparisons.csv</a> · '
    body += '<a href="verification_receipt.json">verification receipt</a> · <a href="report.md">Markdown</a></p>'
    body += (
        '<p class="muted">Link, CSV and raw hash verification is recorded; browser visual rendering is not claimed.</p>'
    )
    (output / "report.html").write_text(_document("MAIN versus COMPILE: campaign and repeat", body), encoding="utf-8")
    lines = [
        "# MAIN versus COMPILE: campaign and repeat",
        "",
        f"**{result['status']}** — {recommendation['text']}",
        "",
        recommendation["limitations"],
        "",
        result["replication"]["description"],
        "",
        "| Metric | Run 1 mean delta ms | Run 2 mean delta ms | Direction consistent |",
        "|---|---:|---:|---|",
    ]
    lines.extend(
        "| "
        + " | ".join(_number(row[key]) for key in ("metric", "run1_delta_ms", "run2_delta_ms", "consistent_direction"))
        + " |"
        for row in table_rows
    )
    lines.extend(
        ["", "Whole-worker RSS COMPILE−MAIN after idle (MiB): " + str(recommendation["rss_delta_after_idle_mib"]), ""]
    )
    lines.extend(
        f"- [Run {index}]({run['report']}) · [manifest]({run['manifest']})" for index, run in enumerate(runs, start=1)
    )
    lines.extend(
        [
            "",
            "[Analysis](analysis.json) · [Comparisons CSV](comparisons.csv) · [Verification receipt](verification_receipt.json)",
        ]
    )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _json(output / "verification_receipt.json", {})
    receipt = _verify_artifacts(
        output,
        receipt_name="verification_receipt.json",
        csv_counts={"comparisons.csv": len(combined_rows)},
        raw_hashes={},
    )
    receipt["runs"] = [
        {"directory": run["directory"], "raw_sha256": run["raw_sha256"], "analysis_sha256": run["analysis_sha256"]}
        for run in runs
    ]
    receipt["campaigns_valid"] = not errors
    _json(output / "verification_receipt.json", receipt)
    return result
