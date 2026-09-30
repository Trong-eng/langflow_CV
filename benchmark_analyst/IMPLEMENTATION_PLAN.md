# SCRFD Langflow Overhead Implementation Plan

> Execution authorized in chat. Work proceeds in this checkout because it contains the user's uncommitted runtime changes and local workload. No extra approval gate or automatic commits.

**Goal:** measure 1,000 real SCRFD requests per compilation/warm arm, reporting Langflow overhead.
**Architecture:** unchanged API/workload, conservative warm compatibility, opt-in serving-process observation, local runner and offline reports.
**Tech Stack:** Python, requests, uvicorn, pytest, matplotlib, Jupyter.
**Spec:** DESIGN.md.

## Constraints and review focus

- Preserve flow, model, image, auth and response semantics. No debug output for the primary campaign.
- Exclude upload/download and union of four SCRFD node execution intervals; missing timings fail validation.
- Compilation and warm state must be measured in serving process, isolated per slot.
- Failed samples are retained; interrupted campaigns never become VALID or resume silently.
- Secrets are loaded only from environment/ignored credential file; never appear in logs/manifests.

## Tasks

- [x] Warm API compatibility: tests first for safe ChatInput.files overrides, no-op global bindings, request isolation, unsupported tweaks/user bindings fallback. Implement in api/warm_graph.py; run existing warm API tests.
- [x] Worker observer: tests first for timing boundaries, overlapping spans, disabled/auth guards, request correlation. Implement worker_observer.py and replace old worker identity middleware; expose protected snapshots and bounded consume-once request measurements.
- [x] Runner/protocol: tests for balanced 4×1,000 design, request contract, validation and missing timing rejection. Implement protocol.py, runtime.py, benchmark_langflow.py. Preserve attempts; fresh worker each slot.
- [x] Reports/notebook: tests for percentile/contrasts and incomplete/invalid evidence. Implement reporting.py, README.md and an offline-capable Vietnamese notebook.
- [ ] End-to-end: existing-flow preflight, verify reference image, four-arm smoke, freeze sources/config, full 4,000 requests. Review changes and publish local reports only after verification.

## Progress

- Planning: removed old benchmark source; archived recoverable copy outside repo. Preserved ignored inputs and credentials.
- Boundary ruling: user explicitly chose Langflow orchestration overhead excluding all SCRFD processing.
- Warm ruling: extend supported file tweaks and no-op variable binding handling; do not change saved flow to make benchmark pass.
- Actual saved custom nodes triggered extension errors without type rewrites. Extracted the existing event emitter and preserved per-user replay on warm copies; rewrites and unknown metadata still fall back cold. Live arm 01 now hits warm and matches reference pixels.
- Validation before live campaign: 77 benchmark tests passed; focused warm/event tests passed; independent review identified model provenance gaps and both were fixed. Reference image inspected and has three detections.
- Live campaign progress is recorded under runs/*/state.json so this frozen source document does not change during measurement.

- Review remediation: regression tests reproduce user-owned saved-file cache-miss failure and cover full application ASGI timing. Fix scope: template-only LocalFileAccessError falls back cold; benchmark-only factory wraps production app outside all middleware/telemetry. Revalidate via smoke-v2 and main-v2; retain old raw unchanged.
