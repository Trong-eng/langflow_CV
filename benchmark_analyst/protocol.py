"""Measurement contract for real SCRFD requests through Langflow."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ARMS = ("00", "01", "10", "11")


def make_schedule(requests_per_arm: int, blocks: int) -> list[dict]:
    if blocks <= 0 or requests_per_arm <= 0 or requests_per_arm % blocks:
        raise ValueError("requests_per_arm must be positive and divisible by positive blocks")
    # Balanced Latin square: every arm occupies every position once in four blocks.
    orders = [("00", "01", "11", "10"), ("01", "10", "00", "11"), ("10", "11", "01", "00"), ("11", "00", "10", "01")]
    return [
        {"block": block + 1, "arm": arm, "count": requests_per_arm // blocks}
        for block in range(blocks)
        for arm in orders[block % 4]
    ]


def measure_overhead(observed: dict, node_ids: list[str]) -> dict[str, float]:
    total = observed.get("server_total_ms")
    if not observed.get("complete") or observed.get("status_code") != 200 or observed.get("intervals_truncated"):
        raise ValueError("incomplete or failed server response")
    if not isinstance(total, (float, int)) or not math.isfinite(total) or total <= 0:
        raise ValueError("invalid server timing")
    intervals = []
    seen = set()
    for span in observed.get("component_intervals_ms", []):
        start, end = span["start_ms"], span["end_ms"]
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in (start, end)):
            raise ValueError("invalid component interval")
        if not 0 <= start <= end <= total:
            raise ValueError("component interval outside request or reversed")
        if span["node_id"] in node_ids:
            seen.add(span["node_id"])
            intervals.append((start, end))
    if not node_ids or seen != set(node_ids):
        raise ValueError(f"missing SCRFD execution intervals: {sorted(set(node_ids) - seen)}")
    intervals.sort()
    processing, right = 0.0, 0.0
    for start, end in intervals:
        processing += max(0.0, end - max(right, start))
        right = max(right, end)
    return {"server_total_ms": total, "scrfd_processing_ms": processing, "langflow_overhead_ms": total - processing}


def run_payload(config: dict, file_path: str, marker: str, session_id: str) -> dict:
    return {
        "input_type": "chat",
        "output_type": "chat",
        "input_value": marker,
        "session_id": session_id,
        "tweaks": {config["input_node_id"]: {"files": file_path}},
    }


def result_image_path(response: dict, output_node_id: str, flow_id: str, expected_faces: int) -> str:
    matches = [
        node
        for run in response.get("outputs", [])
        for node in run.get("outputs", [])
        if node.get("component_id") == output_node_id
    ]
    if len(matches) != 1:
        raise ValueError("expected exactly one final Chat Output result")
    message = matches[0].get("results", {}).get("message", {})
    data = message.get("data", message)
    if data.get("error") or not isinstance(data.get("text"), str):
        raise ValueError("final message contains an error or no text")
    text = data["text"]
    if not text.startswith(f"Detected {expected_faces} face(s)."):
        raise ValueError("unexpected SCRFD face count")
    image = re.search(r"!\[[^\]]*\]\(([^\s)]+)\)", text)
    path = image.group(1) if image else ""
    prefix = f"/api/v1/files/images/{flow_id}/"
    if not path.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9_.-]+", path[len(prefix) :]):
        raise ValueError("missing or unsafe output image path")
    return path


def pixel_digest(content: bytes) -> str:
    from PIL import Image

    with Image.open(io.BytesIO(content)) as image:
        image = image.convert("RGB")
        if image.width <= 0 or image.height <= 0:
            raise ValueError("empty result image")
        return hashlib.sha256(f"{image.width}x{image.height}:RGB:".encode() + image.tobytes()).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def flow_digest(flow: dict) -> str:
    return hashlib.sha256(json.dumps(flow["data"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_model_binding(flow: dict, config: dict) -> None:
    nodes = [node for node in flow["data"]["nodes"] if node.get("data", {}).get("type") == "SCRFDInference"]
    if len(nodes) != 1 or nodes[0]["id"] not in config["scrfd_node_ids"]:
        raise ValueError("model provenance requires exactly one measured SCRFDInference node")
    field = nodes[0]["data"]["node"]["template"].get("model_path", {})
    value = field.get("value")
    if field.get("load_from_db") or not isinstance(value, str) or not value:
        raise ValueError("model provenance requires a fixed model_path in the saved flow")
    if Path(value).expanduser().resolve() != Path(config["model_path"]).resolve():
        raise ValueError("model_path does not match the saved inference node")


def source_identity(root: Path) -> dict:
    # Include dirty and untracked source; ignored runs, credentials and bytecode never enter manifests.
    paths = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root)
    selected = sorted(
        {
            name.decode()
            for name in paths.split(b"\0")
            if name
            and (
                name.startswith((b"benchmark_analyst/", b"src/backend/base/langflow/", b"src/lfx/src/lfx/"))
                or name in (b"pyproject.toml", b"uv.lock")
            )
        }
    )
    hashes = {name: file_digest(root / name) for name in selected if (root / name).is_file()}
    return {
        "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "files": hashes,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
