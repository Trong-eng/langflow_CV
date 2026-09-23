import csv
import json
import math
import mimetypes
import os
import re
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

# ============================================================
# 1. CONFIG
# ============================================================

BASE_URL = "http://localhost:3000"
FLOW_ID = "d7b1b956-0181-472e-8d6f-ccf7e55a4066"
API_KEY = os.environ.get("LANGFLOW_API_KEY")
if not API_KEY:
    raise RuntimeError("Set LANGFLOW_API_KEY before running the benchmark.")

RUN_URL = f"{BASE_URL}/api/v1/run/{FLOW_ID}?stream=false"
TRACES_URL = f"{BASE_URL}/api/v1/monitor/traces"
FLOW_URL = f"{BASE_URL}/api/v1/flows/{FLOW_ID}"
UPLOAD_URL = f"{BASE_URL}/api/v1/files/upload/{FLOW_ID}"
DELETE_FILE_BASE_URL = f"{BASE_URL}/api/v1/files/delete/{FLOW_ID}"

IMAGE_PATH = Path(
    "/Users/tranquangtrong/Library/Caches/langflow/"
    "d7b1b956-0181-472e-8d6f-ccf7e55a4066/"
    "2026-09-17_14-11-03_face_detection_example.jpg"
)

N_WARMUP = 5
N_RUNS = 100
REQUEST_TIMEOUT = 60
TRACE_WAIT_TIMEOUT = 5.0
TRACE_POLL_INTERVAL = 0.05

# Xóa ảnh INPUT vừa upload sau khi run + lấy trace xong để storage không bị đầy.
# Thời gian cleanup KHÔNG được tính vào benchmark.
CLEANUP_UPLOADED_INPUT = True


# ============================================================
# 2. COMPONENTS TRONG FLOW
# ============================================================

COMPONENT_ALIASES = {
    "chat_input_ms": ["Chat Input", "ChatInput"],
    "scrfd_preprocess_ms": ["SCRFD Preprocess", "SCRFDPreprocess"],
    "scrfd_inference_ms": ["SCRFD Inference", "SCRFDInference"],
    "scrfd_draw_detections_ms": ["SCRFD Draw Detections", "SCRFDDrawDetections"],
    "detection_image_output_ms": ["Detection Image Output", "DetectionImageOutput"],
    "chat_output_ms": ["Chat Output", "ChatOutput"],
}


# ============================================================
# 3. OUTPUT FILES
# ============================================================

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
RUNS_CSV = Path(f"langflow_real_upload_runs_{timestamp}.csv")
SUMMARY_CSV = Path(f"langflow_real_upload_summary_{timestamp}.csv")


# ============================================================
# 4. INPUT IMAGE
# ============================================================

if not IMAGE_PATH.exists():
    raise FileNotFoundError(f"Không tìm thấy ảnh:\n{IMAGE_PATH}")

image_bytes = IMAGE_PATH.read_bytes()
mime_type, _ = mimetypes.guess_type(str(IMAGE_PATH))
if mime_type is None:
    mime_type = "image/jpeg"


# ============================================================
# 5. HTTP SESSION
# ============================================================

# Không set Content-Type globally vì upload cần multipart/form-data
# với boundary do requests tự tạo.
http = requests.Session()
http.headers.update(
    {
        "x-api-key": API_KEY,
        "accept": "application/json",
    }
)

warmup_session_id = "scrfd-real-upload-warmup-" + str(uuid.uuid4())
benchmark_session_id = "scrfd-real-upload-" + str(uuid.uuid4())


# ============================================================
# 6. STAT / TRACE HELPERS
# ============================================================


def percentile(values, percent):
    values = sorted(values)
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]

    k = (len(values) - 1) * percent / 100.0
    lower = math.floor(k)
    upper = math.ceil(k)

    if lower == upper:
        return values[lower]

    return values[lower] * (upper - k) + values[upper] * (k - lower)


def calculate_stats(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return {
            "count": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "min": None,
            "max": None,
            "std": None,
        }

    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "min": min(values),
        "max": max(values),
        "std": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def normalize_name(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def flatten_spans(spans):
    result = []

    def walk(span):
        if not isinstance(span, dict):
            return
        result.append(span)
        for child in span.get("children") or []:
            walk(child)

    for span in spans or []:
        walk(span)

    return result


def extract_component_latencies(trace_detail):
    spans = flatten_spans(trace_detail.get("spans", []))
    result = {}
    matched_names = {}
    all_span_names = [span.get("name", "") for span in spans]

    for metric_name, aliases in COMPONENT_ALIASES.items():
        aliases_normalized = [normalize_name(alias) for alias in aliases]
        candidates = []

        for span in spans:
            span_name = span.get("name", "")
            normalized = normalize_name(span_name)

            if not any(alias in normalized for alias in aliases_normalized):
                continue

            latency = span.get("latencyMs")
            if latency is None:
                continue

            try:
                latency = float(latency)
            except (TypeError, ValueError):
                continue

            candidates.append((latency, span_name))

        if candidates:
            # Nếu match nhiều span, chọn span bao ngoài có latency lớn nhất.
            candidates.sort(key=lambda x: x[0], reverse=True)
            latency, span_name = candidates[0]
            result[metric_name] = latency
            matched_names[metric_name] = span_name
        else:
            result[metric_name] = None
            matched_names[metric_name] = None

    return result, matched_names, all_span_names


# ============================================================
# 7. AUTO-DETECT CHAT INPUT COMPONENT ID
# ============================================================


def get_chat_input_component_id():
    response = http.get(FLOW_URL, timeout=30)
    response.raise_for_status()
    flow = response.json()

    data = flow.get("data") or {}
    nodes = data.get("nodes") or []

    for node in nodes:
        node_id = (
            node.get("id")
            or (node.get("data") or {}).get("id")
            or ((node.get("data") or {}).get("node") or {}).get("id")
        )

        node_data = node.get("data") or {}
        node_def = node_data.get("node") or {}

        node_type = node_data.get("type") or node_def.get("name") or ""
        display_name = node_data.get("display_name") or node_def.get("display_name") or ""

        if str(node_id).startswith("ChatInput-") or node_type == "ChatInput" or display_name == "Chat Input":
            return node_id

    raise RuntimeError("Không tìm thấy Chat Input component ID trong flow.")


# ============================================================
# 8. PREFLIGHT
# ============================================================


def preflight():
    print("=" * 86)
    print("PREFLIGHT")
    print("=" * 86)

    trace_test = http.get(
        TRACES_URL,
        params={"flow_id": FLOW_ID, "page": 1, "size": 1},
        timeout=15,
    )
    print("Trace API HTTP :", trace_test.status_code)
    trace_test.raise_for_status()

    chat_input_id = get_chat_input_component_id()
    print("Chat Input ID  :", chat_input_id)
    print("Upload URL     :", UPLOAD_URL)
    print()
    return chat_input_id


# ============================================================
# 9. REAL USER-LIKE FILE UPLOAD
# ============================================================


def upload_image(marker):
    """Mỗi request upload ảnh thật qua /files/upload/{flow_id}.
    Đây là bước tương đương user chọn/upload ảnh trong Playground.
    """
    suffix = IMAGE_PATH.suffix or ".jpg"
    unique_name = f"{marker}_{uuid.uuid4().hex}{suffix}"

    start_ns = time.perf_counter_ns()

    # Không gửi Content-Type thủ công; requests tự tạo multipart boundary.
    response = http.post(
        UPLOAD_URL,
        files={
            "file": (
                unique_name,
                image_bytes,
                mime_type,
            )
        },
        timeout=REQUEST_TIMEOUT,
    )

    end_ns = time.perf_counter_ns()
    upload_latency_ms = (end_ns - start_ns) / 1_000_000

    response.raise_for_status()
    payload = response.json()

    file_path = payload.get("file_path")
    if not file_path:
        raise RuntimeError(f"Upload thành công nhưng không có file_path: {payload}")

    return upload_latency_ms, file_path


def cleanup_uploaded_file(file_path):
    if not CLEANUP_UPLOADED_INPUT or not file_path:
        return

    file_name = str(file_path).split("/")[-1]

    try:
        response = http.delete(
            f"{DELETE_FILE_BASE_URL}/{file_name}",
            timeout=20,
        )
        # Cleanup không thuộc benchmark, nên không fail cả benchmark nếu xóa lỗi.
        if response.status_code >= 400:
            print(f"  [cleanup warning] HTTP {response.status_code} for {file_name}")
    except requests.RequestException as exc:
        print(f"  [cleanup warning] {exc}")


# ============================================================
# 10. RUN FLOW WITH THE FRESHLY UPLOADED FILE
# ============================================================


def run_flow(session_id, marker, file_path, chat_input_id):
    """Quan trọng:
    - KHÔNG gửi top-level 'files' nữa.
    - file_path vừa upload được truyền đúng vào Chat Input qua tweaks.
    """
    payload = {
        "output_type": "chat",
        "input_type": "chat",
        # input_value chỉ truyền MỘT lần ở top-level.
        # Nếu đồng thời truyền input_value trong ChatInput tweak,
        # Langflow trả 400: duplicate input_value for Chat Input.
        "input_value": marker,
        "session_id": session_id,
        "tweaks": {
            chat_input_id: {
                # Langflow docs expects the uploaded storage path as a string.
                "files": file_path,
            }
        },
    }

    start_ns = time.perf_counter_ns()

    response = http.post(
        RUN_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )

    end_ns = time.perf_counter_ns()
    flow_api_latency_ms = (end_ns - start_ns) / 1_000_000

    if response.status_code >= 400:
        raise RuntimeError(
            f"Flow run failed: HTTP {response.status_code} | "
            f"response={response.text[:2000]} | payload={json.dumps(payload, ensure_ascii=False)[:2000]}"
        )

    return flow_api_latency_ms, response


def execute_user_like_request(session_id, marker, chat_input_id):
    """User-like E2E được đo từ lúc bắt đầu upload ảnh
    tới khi flow trả response.

    Không bao gồm trace polling và cleanup.
    """
    e2e_start_ns = time.perf_counter_ns()

    upload_latency_ms, file_path = upload_image(marker)
    flow_api_latency_ms, response = run_flow(
        session_id,
        marker,
        file_path,
        chat_input_id,
    )

    e2e_end_ns = time.perf_counter_ns()
    user_e2e_latency_ms = (e2e_end_ns - e2e_start_ns) / 1_000_000

    client_between_steps_ms = user_e2e_latency_ms - upload_latency_ms - flow_api_latency_ms

    return {
        "upload_latency_ms": upload_latency_ms,
        "flow_api_latency_ms": flow_api_latency_ms,
        "user_e2e_latency_ms": user_e2e_latency_ms,
        "client_between_steps_ms": client_between_steps_ms,
        "file_path": file_path,
        "response": response,
    }


# ============================================================
# 11. TRACE LOOKUP
# ============================================================


def find_trace(session_id, marker):
    deadline = time.monotonic() + TRACE_WAIT_TIMEOUT

    while time.monotonic() < deadline:
        response = http.get(
            TRACES_URL,
            params={
                "flow_id": FLOW_ID,
                "session_id": session_id,
                "page": 1,
                "size": 200,
            },
            timeout=30,
        )
        response.raise_for_status()

        traces = response.json().get("traces") or []

        for trace in traces:
            trace_input = trace.get("input") or {}
            if trace_input.get("input_value") == marker:
                return trace

        time.sleep(TRACE_POLL_INTERVAL)

    return None


def get_trace_detail(trace_id):
    deadline = time.monotonic() + TRACE_WAIT_TIMEOUT
    url = f"{TRACES_URL}/{trace_id}"
    last_result = None

    while time.monotonic() < deadline:
        response = http.get(url, timeout=30)
        response.raise_for_status()
        detail = response.json()
        last_result = detail

        if detail.get("spans"):
            return detail

        time.sleep(TRACE_POLL_INTERVAL)

    return last_result


# ============================================================
# 12. START
# ============================================================

print()
print("=" * 86)
print("LANGFLOW SCRFD — REAL UPLOAD + OVERHEAD BENCHMARK")
print("=" * 86)
print(f"Flow       : {FLOW_ID}")
print(f"Image      : {IMAGE_PATH.name}")
print(f"Image size : {len(image_bytes) / 1024:.2f} KB")
print(f"Warm-up    : {N_WARMUP}")
print(f"Runs       : {N_RUNS}")
print()
print("Mỗi run: upload ảnh thật -> Chat Input tweak -> flow -> response")
print("Internal orchestration = Trace Total - Sum(Component Latencies)")
print()

CHAT_INPUT_ID = preflight()


# ============================================================
# 13. WARM-UP — CŨNG UPLOAD ẢNH MỚI MỖI LẦN
# ============================================================

print("=" * 86)
print("WARM-UP")
print("=" * 86)

for i in range(N_WARMUP):
    marker = f"warmup-real-upload-{i + 1:03d}"
    file_path = None

    try:
        result = execute_user_like_request(
            warmup_session_id,
            marker,
            CHAT_INPUT_ID,
        )
        file_path = result["file_path"]

        print(
            f"Warm-up {i + 1:02d}/{N_WARMUP}"
            f" | Upload {result['upload_latency_ms']:8.2f} ms"
            f" | Flow {result['flow_api_latency_ms']:8.2f} ms"
            f" | User E2E {result['user_e2e_latency_ms']:8.2f} ms"
            f" | OK"
        )
    except Exception as exc:
        print(f"Warm-up failed: {exc}")
        raise SystemExit(1)
    finally:
        cleanup_uploaded_file(file_path)

print()
print("Warm-up completed.")
print()


# ============================================================
# 14. MEASURED RUNS
# ============================================================

print("=" * 86)
print("MEASURED RUNS")
print("=" * 86)

rows = []

for i in range(N_RUNS):
    run_number = i + 1
    marker = f"benchmark-real-upload-{run_number:03d}"
    file_path = None

    row = {
        "run": run_number,
        "marker": marker,
        "success": False,
        "http_status": None,
        "uploaded_file_path": None,
        # User/API timings
        "upload_latency_ms": None,
        "flow_api_latency_ms": None,
        "user_e2e_latency_ms": None,
        "client_between_steps_ms": None,
        # Trace
        "trace_id": None,
        "trace_total_latency_ms": None,
        # Components
        "chat_input_ms": None,
        "scrfd_preprocess_ms": None,
        "scrfd_inference_ms": None,
        "scrfd_draw_detections_ms": None,
        "detection_image_output_ms": None,
        "chat_output_ms": None,
        "component_sum_ms": None,
        # Gaps
        "internal_orchestration_overhead_ms": None,
        "internal_orchestration_overhead_percent": None,
        "flow_api_minus_trace_ms": None,
        "user_e2e_minus_trace_ms": None,
        "user_e2e_minus_component_sum_ms": None,
        "component_match_ok": False,
        "error": "",
    }

    try:
        # ----------------------------------------------------
        # A. REAL USER-LIKE REQUEST: UPLOAD + FLOW
        # ----------------------------------------------------
        execution = execute_user_like_request(
            benchmark_session_id,
            marker,
            CHAT_INPUT_ID,
        )

        file_path = execution["file_path"]
        response = execution["response"]

        row["uploaded_file_path"] = file_path
        row["http_status"] = response.status_code
        row["upload_latency_ms"] = execution["upload_latency_ms"]
        row["flow_api_latency_ms"] = execution["flow_api_latency_ms"]
        row["user_e2e_latency_ms"] = execution["user_e2e_latency_ms"]
        row["client_between_steps_ms"] = execution["client_between_steps_ms"]

        # ----------------------------------------------------
        # B. FIND THE TRACE OF THIS EXACT RUN
        # ----------------------------------------------------
        trace = find_trace(benchmark_session_id, marker)
        if trace is None:
            raise RuntimeError("Could not find trace for this request.")

        trace_id = trace.get("id")
        row["trace_id"] = trace_id

        # ----------------------------------------------------
        # C. TRACE DETAIL
        # ----------------------------------------------------
        detail = get_trace_detail(trace_id)
        if not detail:
            raise RuntimeError("Could not retrieve trace detail.")

        trace_total = detail.get("totalLatencyMs")
        if trace_total is None:
            raise RuntimeError("totalLatencyMs missing from trace.")

        trace_total = float(trace_total)
        row["trace_total_latency_ms"] = trace_total

        # ----------------------------------------------------
        # D. COMPONENT LATENCIES
        # ----------------------------------------------------
        components, matched_names, all_span_names = extract_component_latencies(detail)

        for key, value in components.items():
            row[key] = value

        missing_components = [key for key, value in components.items() if value is None]

        if not missing_components:
            row["component_match_ok"] = True

            component_sum = sum(components.values())
            row["component_sum_ms"] = component_sum

            # Trace internal orchestration/framework gap
            internal_overhead = trace_total - component_sum
            row["internal_orchestration_overhead_ms"] = internal_overhead

            if trace_total > 0:
                row["internal_orchestration_overhead_percent"] = internal_overhead / trace_total * 100

            # Flow API outer gap; upload KHÔNG nằm trong trace
            row["flow_api_minus_trace_ms"] = row["flow_api_latency_ms"] - trace_total

            # User experience gap including fresh file upload
            row["user_e2e_minus_trace_ms"] = row["user_e2e_latency_ms"] - trace_total

            row["user_e2e_minus_component_sum_ms"] = row["user_e2e_latency_ms"] - component_sum
        else:
            debug_file = Path(f"trace_debug_real_upload_run_{run_number:03d}.json")
            debug_file.write_text(
                json.dumps(
                    {
                        "missing_components": missing_components,
                        "matched_names": matched_names,
                        "all_span_names": all_span_names,
                        "trace": detail,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            row["error"] = f"Missing component spans: {missing_components}. See {debug_file}"

        row["success"] = True

    except Exception as exc:
        row["error"] = str(exc)

    finally:
        # Cleanup sau khi flow + trace đã xong. Không tính vào latency.
        cleanup_uploaded_file(file_path)

    rows.append(row)

    def fmt(value):
        return f"{value:7.2f}" if value is not None else "    N/A"

    print(
        f"Run {run_number:03d}/{N_RUNS}"
        f" | Upload {fmt(row['upload_latency_ms'])} ms"
        f" | Flow {fmt(row['flow_api_latency_ms'])} ms"
        f" | UserE2E {fmt(row['user_e2e_latency_ms'])} ms"
        f" | Trace {fmt(row['trace_total_latency_ms'])} ms"
        f" | Comp {fmt(row['component_sum_ms'])} ms"
        f" | Orch {fmt(row['internal_orchestration_overhead_ms'])} ms"
    )


# ============================================================
# 15. CSV 1 — PER RUN
# ============================================================

RUN_COLUMNS = [
    "run",
    "marker",
    "success",
    "http_status",
    "uploaded_file_path",
    "upload_latency_ms",
    "flow_api_latency_ms",
    "user_e2e_latency_ms",
    "client_between_steps_ms",
    "trace_id",
    "trace_total_latency_ms",
    "chat_input_ms",
    "scrfd_preprocess_ms",
    "scrfd_inference_ms",
    "scrfd_draw_detections_ms",
    "detection_image_output_ms",
    "chat_output_ms",
    "component_sum_ms",
    "internal_orchestration_overhead_ms",
    "internal_orchestration_overhead_percent",
    "flow_api_minus_trace_ms",
    "user_e2e_minus_trace_ms",
    "user_e2e_minus_component_sum_ms",
    "component_match_ok",
    "error",
]

with RUNS_CSV.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=RUN_COLUMNS)
    writer.writeheader()

    for row in rows:
        output_row = {}
        for key in RUN_COLUMNS:
            value = row.get(key)
            if isinstance(value, float):
                value = round(value, 4)
            output_row[key] = value
        writer.writerow(output_row)


# ============================================================
# 16. CSV 2 — SUMMARY
# ============================================================

METRICS = {
    "upload_latency_ms": "Fresh image upload latency via /files/upload/{flow_id}",
    "flow_api_latency_ms": "Flow POST /run latency after image has been freshly uploaded",
    "user_e2e_latency_ms": "Realistic client latency: fresh upload + flow run + response",
    "trace_total_latency_ms": "Langflow internal full-flow trace latency",
    "chat_input_ms": "Chat Input component latency",
    "scrfd_preprocess_ms": "SCRFD Preprocess component latency",
    "scrfd_inference_ms": "SCRFD Inference component latency",
    "scrfd_draw_detections_ms": "SCRFD Draw Detections component latency",
    "detection_image_output_ms": "Detection Image Output component latency",
    "chat_output_ms": "Chat Output component latency",
    "component_sum_ms": "Sum of six component latencies",
    "internal_orchestration_overhead_ms": "Trace Total - Component Sum (internal Langflow orchestration/framework gap)",
    "internal_orchestration_overhead_percent": "Internal orchestration gap as percentage of trace total",
    "flow_api_minus_trace_ms": "Flow POST latency - trace total (outer flow/API gap, excludes upload)",
    "user_e2e_minus_trace_ms": "User E2E - trace total (includes fresh upload + outer flow/API gap)",
    "user_e2e_minus_component_sum_ms": "User E2E - component sum",
}

summary_rows = []

for metric, description in METRICS.items():
    values = [row.get(metric) for row in rows if row.get(metric) is not None]
    stats = calculate_stats(values)

    summary_rows.append(
        {
            "metric": metric,
            "description": description,
            "count": stats["count"],
            "mean": stats["mean"],
            "p50": stats["p50"],
            "p95": stats["p95"],
            "min": stats["min"],
            "max": stats["max"],
            "std": stats["std"],
        }
    )

SUMMARY_COLUMNS = [
    "metric",
    "description",
    "count",
    "mean",
    "p50",
    "p95",
    "min",
    "max",
    "std",
]

with SUMMARY_CSV.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
    writer.writeheader()

    for row in summary_rows:
        output_row = dict(row)
        for key in ["mean", "p50", "p95", "min", "max", "std"]:
            if output_row[key] is not None:
                output_row[key] = round(output_row[key], 4)
        writer.writerow(output_row)


# ============================================================
# 17. FINAL REPORT
# ============================================================

valid_rows = [row for row in rows if row.get("internal_orchestration_overhead_ms") is not None]

print()
print("=" * 86)
print("FINAL REAL-UPLOAD BENCHMARK RESULT")
print("=" * 86)
print(f"Valid measured runs: {len(valid_rows)}/{N_RUNS}")

if valid_rows:

    def stats_for(key):
        return calculate_stats([row[key] for row in valid_rows if row.get(key) is not None])

    upload_stats = stats_for("upload_latency_ms")
    flow_stats = stats_for("flow_api_latency_ms")
    user_e2e_stats = stats_for("user_e2e_latency_ms")
    trace_stats = stats_for("trace_total_latency_ms")
    component_stats = stats_for("component_sum_ms")
    orchestration_stats = stats_for("internal_orchestration_overhead_ms")
    outer_flow_stats = stats_for("flow_api_minus_trace_ms")

    print()
    print("USER-LIKE REQUEST")
    print(f"Upload mean          : {upload_stats['mean']:.2f} ms")
    print(f"Flow API mean        : {flow_stats['mean']:.2f} ms")
    print(f"User E2E mean        : {user_e2e_stats['mean']:.2f} ms")

    print()
    print("LANGFLOW INTERNAL TRACE")
    print(f"Trace total mean     : {trace_stats['mean']:.2f} ms")
    print(f"Component sum mean   : {component_stats['mean']:.2f} ms")
    print(f"Orchestration mean   : {orchestration_stats['mean']:.2f} ms")

    print()
    print("ORCHESTRATION DISTRIBUTION")
    print(f"Mean : {orchestration_stats['mean']:.2f} ms")
    print(f"P50  : {orchestration_stats['p50']:.2f} ms")
    print(f"P95  : {orchestration_stats['p95']:.2f} ms")
    print(f"Min  : {orchestration_stats['min']:.2f} ms")
    print(f"Max  : {orchestration_stats['max']:.2f} ms")
    print(f"Std  : {orchestration_stats['std']:.2f} ms")

    print()
    print("MEAN DECOMPOSITION")
    print(
        f"Trace: {trace_stats['mean']:.2f} ms"
        f" = Components {component_stats['mean']:.2f} ms"
        f" + Orchestration {orchestration_stats['mean']:.2f} ms"
    )
    print(
        f"Flow API: {flow_stats['mean']:.2f} ms"
        f" = Trace {trace_stats['mean']:.2f} ms"
        f" + Outside-trace {outer_flow_stats['mean']:.2f} ms"
    )
    print(
        f"User E2E: {user_e2e_stats['mean']:.2f} ms"
        f" ≈ Upload {upload_stats['mean']:.2f} ms"
        f" + Flow API {flow_stats['mean']:.2f} ms"
    )

    negatives = [
        row["internal_orchestration_overhead_ms"] for row in valid_rows if row["internal_orchestration_overhead_ms"] < 0
    ]
    if negatives:
        print()
        print(
            f"WARNING: {len(negatives)} run(s) have negative internal overhead. "
            "Possible overlapping spans or span matching issue."
        )
else:
    print("Không tính được overhead. Kiểm tra trace_debug_real_upload_run_XXX.json")

print()
print("Per-run CSV :", RUNS_CSV.resolve())
print("Summary CSV :", SUMMARY_CSV.resolve())
print("=" * 86)
