# MAIN versus COMPILE benchmark implementation plan

> **For agentic workers:** Use the provided worker specification and parallel task ownership below. Execution is already authorized by the user.

**Goal:** Measure whether compilation caching improves real warmed local-main latency and quantify whole-worker RAM tradeoffs.

**Architecture:** Pin local main c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d in two native worktrees. Keep baseline byte-identical. Apply only four cache runtime files plus focused tests in COMPILE. A common external observer wraps production ASGI and real component output bodies identically; each worker uses the same installed interpreter and frozen fixture clones.

**Tech stack:** Python, FastAPI/uvicorn, requests, psutil, pytest, matplotlib.

**Spec:** /Users/tranquangtrong/.codex/attachments/432fdafe-f993-4d4e-a026-17c4980d1354/Pasted text.txt

## Fixed constraints

- Local main commit pinned, no origin substitution, push, merge, deploy, or changes to source DB/storage.
- Warm registry OFF both groups; preserve original main warm code.
- Four blocks, orders MAIN→COMPILE, COMPILE→MAIN, COMPILE→MAIN, MAIN→COMPILE; 250 measured/slot, five warmups/worker; repeat once with new clones/workers.
- One worker/concurrency one. Upload/output checks/control snapshots/RSS outside respective timing.
- Same frozen flow/image/model/reference, same dependencies/profile, per-group source identities and matching smoke.
- Fresh namespaces; preserve errors and all adverse observations, stop on validity failure.
- Four checkpoints, RSS whole worker, USS optional null/reason. Blocks are repetition units.

## Review focus

Wrong editable import; MAIN secretly refactored; smoke accepts changed source/profile; incorrect output/counters/timing; failed workers/raw omitted or overwritten.

## Tasks and ownership

- [x] cache_only: four runtime files in COMPILE plus tests, red baseline check then focused regression and ruff; commit codex/benchmark-compile-only-v6. Review MAIN→COMPILE diff excludes warm changes.
- [x] harness_audit: new revision_worker.py/revision_campaign.py and tests. External transparent instrumentation, imports/digests/dependencies, null baseline cache counters, effective profile binding, balanced fresh slots, strict source-aware smoke and no overwrite, preservation/cleanup tests.
- [x] reporting: new revision_reporting.py and tests. Actual server/client latency plus overhead, block deltas, warmups, all memory checkpoints, repeat consistency, CSV/PNG/HTML/MD/manifests/receipts; invalid runs cannot support benefits.
- [x] root: protect existing dirty/untracked and historical v3/v4/v5 hashes; verify fixture/reference and dependencies; self-review implementation and independent cross-review; all tests before measuring.
- [x] root: fresh smoke all eight slots, verify valid; campaign 1 and repeat sequential, no tests/analyzers/probes while measuring; retain failed attempts and rerun smoke if any code changes.
- [x] root: offline reports, rendered HTML if supported, links/CSV/PNG/hashes/count/source/dependency/cleanup/historical verification and final evidence-based recommendation.

## Rulings

User supplied complete experimental design and authorized end-to-end execution; no plan/design reconfirmation is necessary. Existing shared dependency environment is reused without syncing to avoid changing the benchmark dependency set; missing package test dependencies would use an isolated environment. No runtime edits are made to MAIN. Harness and reporting are added as separate new files in the existing feature checkout; user files remain unchanged.

## Completion evidence

- COMPILE is clean at c20b6591292e8c3e979db59f831987f3bc4cfbf0; MAIN remains the pinned local main commit. Only four runtime cache files differ; two additional changed files are tests.
- Focused MAIN tests: 330 passed, 2 skipped, 1 xfailed. COMPILE: 358 passed, 2 skipped, 1 xfailed. Final harness/reporting suite: 363 passed. Ruff checks pass.
- Smoke, campaign and repeat are VALID. Primary campaigns contain 4,000 measured requests and 80 warmups, all output checks pass. Four balanced blocks per campaign are the repetition units.
- User presentation steering selects mean, p95 and whole-worker RAM. Fresh `*-mean-p95` reports retain raw evidence and original frozen collector/analyzer. The pre-existing mean/p95 retention criterion is unchanged.
- Server mean improves 6.60% / 7.19%; pooled p95 improves 7.91% / 8.87%. Block 4 p95 is adverse in both campaigns and remains visible in charts/tables. After-idle RSS increases 87.10 / 206.67 MiB; USS is unavailable with AccessDenied.
- Independent arithmetic/link/CSV/PNG/hash review passes. Installed Google Chrome renders all three HTML reports plus the aggregate mobile view with no console errors, missing images or page horizontal overflow.
- Final source/protected-data audit preserves 18 dirty files, 20 pre-existing untracked files and 1,209 historical files. All 24 worker generations (including smoke) stopped and owned clones removed. One empty residual directory skeleton was removed only after verifying no files and stopped worker generation; receipt retained.
- Detailed evidence is under `benchmark_analyst/runs/setup-main-compile-v6`; final receipt is `benchmark_analyst/runs/main-compile-v6-comparison-mean-p95/final_verification_receipt.json`. No push, merge or deployment performed.
