# Component Compilation Artifact Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a disabled-by-default, bounded, process-local cache for source-derived custom-component compilation artifacts while preserving fresh runtime namespaces, classes, constructors, policy checks, and instances.

**Architecture:** A focused `lfx.custom.component_compilation_cache` module owns an exact-source-verified LRU keyed by SHA-256 plus an artifact-generation identifier. `eval_custom_component_code` obtains a prepared immutable artifact, while `create_class` deep-copies its AST template and continues to import and execute module/class code on every call. The benchmark harness exercises the same public evaluation and graph-instantiation paths in baseline, cache-off, and cache-on configurations.

**Tech Stack:** Python 3.13, `ast`, `hashlib`, `threading.RLock`, Pydantic settings, pytest, `resource`, `time.perf_counter_ns`.

**Spec:** `/Users/tranquangtrong/.codex/attachments/4e82310d-549f-4126-8e4d-eec70f49fe50/pasted-text.txt`

## Global Constraints

- Work directly in `/Users/tranquangtrong/Desktop/langflow_CV` on `codex/cache-component-compilation-artifacts`; do not create a worktree or clone.
- Baseline production commit is `c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d`.
- Cache is process-local, bounded, disabled by default, and bypasses sources above 262,144 UTF-8 bytes.
- Cache entries contain no credentials, parameters, user/session objects, graph/vertex references, runtime namespaces, classes, instances, or outputs.
- Trusted-source resolution, runtime imports, module/class-body execution, annotation enforcement, constructor execution, and instance creation remain on every request.
- Use `uv run` for Python commands and do not push, merge, or deploy.

---

### Task 1: Reproducible Baseline Benchmark

**Files:**
- Create: `scripts/benchmarks/benchmark_component_compilation_cache.py`
- Create: `benchmark_results/component_compilation_cache/baseline.json`

**Interfaces:**
- Consumes: `lfx.custom.eval.eval_custom_component_code`, `lfx.interface.initialize.loading.instantiate_class`, and `lfx.graph.graph.base.Graph._instantiate_components_in_vertices`.
- Produces: CLI `--mode baseline|off|on`, JSON containing environment, sample counts, p50/p95, parse/compile counters, cache counters when available, CPU profile totals, and RSS deltas.

- [ ] **Step 1: Add the benchmark harness**

  Implement deterministic same-source, distinct-source, 10-node, 100-node, sequential, concurrent, source-update, cold/miss/hit, constructor, graph-preparation, and local pass-through workloads. Use warm-up iterations outside measured samples, literal sample counts in metadata, and no network/model calls.

- [ ] **Step 2: Run the harness against baseline production code**

  Run: `uv run python scripts/benchmarks/benchmark_component_compilation_cache.py --mode baseline --output benchmark_results/component_compilation_cache/baseline.json`

  Expected: exit 0, valid JSON, and no cache counters in baseline mode.

- [ ] **Step 3: Commit benchmark-only changes**

  Run: `uv run git commit -m "perf: add component compilation benchmark harness"`

### Task 2: Cache Settings and Core LRU Behavior

**Files:**
- Create: `src/lfx/src/lfx/custom/component_compilation_cache.py`
- Modify: `src/lfx/src/lfx/services/settings/groups/cache.py`
- Modify: `src/lfx/tests/unit/services/settings/test_settings_composition.py`
- Create: `src/lfx/tests/unit/custom/test_component_compilation_cache.py`

**Interfaces:**
- Consumes: `Settings.component_compilation_cache_enabled`, exact source strings, and a zero-argument artifact builder.
- Produces: `get_or_build_component_artifact(source, builder)`, `clear_component_compilation_cache()`, `component_compilation_cache_stats()`, `COMPONENT_COMPILATION_ARTIFACT_GENERATION`, and an immutable `ComponentCompilationArtifact`.

- [ ] **Step 1: Write failing settings and cache behavior tests**

  Cover default-off/env-on behavior, first miss/second hit, different source, same class name with different source, source update, LRU eviction, clear, oversize bypass, disabled bypass, invalid builder errors not cached, generation invalidation, exact-source verification, and concurrent same-source construction once.

- [ ] **Step 2: Verify tests fail for missing APIs**

  Run: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/services/settings/test_settings_composition.py -q`

  Expected: FAIL because the new setting/module/API does not exist.

- [ ] **Step 3: Implement the bounded process-local cache**

  Add `component_compilation_cache_enabled: bool = False`; use a 128-entry `OrderedDict`, SHA-256 full digest plus generation key, stored exact source comparison, a 262,144-byte limit, `RLock`, build-under-lock single-flight behavior, no error entries, and counters for hits/misses/bypasses/evictions/builds.

- [ ] **Step 4: Verify focused tests pass**

  Run: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/services/settings/test_settings_composition.py -q`

  Expected: PASS.

### Task 3: Integrate Artifacts Without Reusing Runtime State

**Files:**
- Modify: `src/lfx/src/lfx/custom/eval.py`
- Modify: `src/lfx/src/lfx/custom/validate.py`
- Modify: `src/lfx/tests/unit/custom/test_component_compilation_cache.py`
- Modify: `src/lfx/tests/unit/custom/component/test_validate.py`

**Interfaces:**
- Consumes: `ComponentCompilationArtifact` with normalized source, class name, AST template, future imports, compiled target-class code, and trusted vector-store decorator alias.
- Produces: `prepare_component_compilation_artifact(code, class_name=None)` and `create_class(code, class_name, *, artifact=None)` while preserving the existing public calls.

- [ ] **Step 1: Write failing integration and isolation tests**

  Cover parse/compile count reduction, fresh class identity, fresh function globals, mutable class/global isolation, decorators and helper side effects on every call, imports, inheritance, constructor/user/parameter behavior, independent component inputs/outputs, concurrent isolation, weak-reference release of graph/request objects, annotation rejection, and a warmed cache followed by a runtime policy denial before eval.

- [ ] **Step 2: Verify integration tests fail for absent artifact reuse**

  Run: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/custom/component/test_validate.py src/lfx/tests/unit/custom/test_annotation_validation.py src/lfx/tests/unit/utils/test_resolve_trusted_code_for_build.py -q`

  Expected: new cache assertions FAIL while existing compatibility/security tests remain green.

- [ ] **Step 3: Implement artifact preparation and per-call materialization**

  Move only source transforms, AST parsing, static annotation validation, future-import insertion, trusted decorator analysis, and target-class compilation into artifact construction. Deep-copy the cached AST before every `prepare_global_scope` call; rebuild `exec_globals`, helper classes/functions, the component class, annotation sidecars, and vector-store decoration every time. Never hold the cache lock during `prepare_global_scope`, `exec`, class construction, or instance construction.

- [ ] **Step 4: Verify integration and security suites pass**

  Run: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/custom/component/test_validate.py src/lfx/tests/unit/custom/test_annotation_validation.py src/lfx/tests/unit/interface/test_loading_custom_component_code_param.py src/lfx/tests/unit/utils/test_resolve_trusted_code_for_build.py src/backend/tests/unit/api/test_warm_graph_execution.py src/backend/tests/unit/api/v1/test_custom_component_policy.py -q`

  Expected: PASS.

### Task 4: Benchmark OFF and ON, Document Results

**Files:**
- Modify: `scripts/benchmarks/benchmark_component_compilation_cache.py`
- Create: `benchmark_results/component_compilation_cache/off.json`
- Create: `benchmark_results/component_compilation_cache/on.json`
- Create: `benchmark_results/component_compilation_cache/report.md`

**Interfaces:**
- Consumes: the final cache clear/stats API and `LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED`.
- Produces: comparable absolute and percentage results for baseline/OFF/ON, including p50/p95, parse/compile counts, RSS, cold-miss overhead, concurrent and graph-size workloads.

- [ ] **Step 1: Run final code with cache disabled**

  Run: `LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED=false uv run python scripts/benchmarks/benchmark_component_compilation_cache.py --mode off --output benchmark_results/component_compilation_cache/off.json`

  Expected: exit 0 and cache entries remain zero.

- [ ] **Step 2: Run final code with cache enabled**

  Run: `LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED=true uv run python scripts/benchmarks/benchmark_component_compilation_cache.py --mode on --output benchmark_results/component_compilation_cache/on.json`

  Expected: exit 0, same-source hits occur, and source updates miss.

- [ ] **Step 3: Write the measured comparison report**

  Generate a Markdown table from the JSON outputs with absolute p50/p95, percentage deltas, CPU/profile summary, parse/compile counts, hit/miss/bypass/eviction counts, RSS cost, Python version, worker count, sample count, limitations, and a deployment recommendation based only on measured data.

### Task 5: Final Verification and Diff Review

**Files:**
- Modify only files listed by Tasks 1-4 as required by formatting.

**Interfaces:**
- Consumes: completed implementation and benchmark artifacts.
- Produces: verified branch ready for user review, without push/merge/deploy.

- [ ] **Step 1: Format backend changes**

  Run: `make format_backend`

  Expected: exit 0.

- [ ] **Step 2: Run focused tests again**

  Run: `uv run pytest src/lfx/tests/unit/custom/test_component_compilation_cache.py src/lfx/tests/unit/custom/component/test_validate.py src/lfx/tests/unit/custom/test_annotation_validation.py src/lfx/tests/unit/interface/test_loading_custom_component_code_param.py src/lfx/tests/unit/utils/test_resolve_trusted_code_for_build.py src/lfx/tests/unit/services/settings/test_settings_composition.py src/backend/tests/unit/api/test_warm_graph_execution.py src/backend/tests/unit/api/v1/test_custom_component_policy.py -q`

  Expected: PASS.

- [ ] **Step 3: Run lint/type checks scoped to changed Python files**

  Run: `uv run ruff check src/lfx/src/lfx/custom/component_compilation_cache.py src/lfx/src/lfx/custom/eval.py src/lfx/src/lfx/custom/validate.py src/lfx/src/lfx/services/settings/groups/cache.py src/lfx/tests/unit/custom/test_component_compilation_cache.py scripts/benchmarks/benchmark_component_compilation_cache.py`

  Expected: PASS.

- [ ] **Step 4: Review the final diff and repository state**

  Run: `git diff --check && git diff --stat c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d && git status --short --branch`

  Expected: no whitespace errors and no files outside the planned scope.

