# Component compilation artifact cache benchmark

## Scope and reproducibility

- Repository: `/Users/tranquangtrong/Desktop/langflow_CV`
- Branch: `codex/cache-component-compilation-artifacts`
- Baseline SHA: `c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d`
- Implementation SHA measured by OFF/ON: `b133c5035`
- Python: 3.13.14, macOS 26.6.2 arm64, one worker
- Latency samples: 40 component evaluations and 15 graph runs after 5 warm-up iterations
- Concurrent workloads: 32 requests on 8 threads; 15 batches for distinct-source p50/p95
- Memory workload: 128 unique sources in a separate process measurement
- Benchmark component: local pass-through component; no model, network, or external-service calls

The same harness, `scripts/benchmarks/benchmark_component_compilation_cache.py`, produced the committed `baseline.json`, `off.json`, and `on.json` files. Each JSON records `metadata.source_revision`; baseline mode rejects any revision other than the fixed baseline SHA. The baseline run used `git archive c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d` in a temporary directory, copied the same harness into that archive, and ran it with `--mode baseline --source-revision c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d`. OFF/ON used `--source-revision b133c5035`. This avoids checkout changes while making a wrong-revision baseline fail fast.

## Execution path

Before:

```text
resolve trusted source -> parse/validate/compile -> fresh namespace/exec -> fresh class -> fresh instance
```

After, with the feature enabled:

```text
resolve trusted source -> cache key + lookup
                         | miss: parse/validate/compile -> retain source artifact
                         | hit:  load source artifact
                       -> fresh AST -> fresh namespace/exec -> fresh class -> fresh instance
```

Trusted-source resolution and execution policy checks remain before evaluation. A cache hit does not reuse the execution namespace, class object, component instance, parameters, user/session bindings, runtime outputs, or tracing state. Imports, source helper definitions, class-body execution, decorators, annotation registration, and the constructor still execute for every evaluation.

## Cache design

The cached artifact contains only:

- the extracted component class name;
- a serialized immutable AST template, deserialized to a fresh AST on hits;
- the compiled target-class code object;
- statically proven trusted vector-store decorator metadata.

The key is `(artifact generation, SHA-256(full resolved source), class-selection variant)`. The entry and artifact also retain and compare the exact source, and artifact consumption checks its generation, so even a digest collision or incorrectly paired internal call cannot execute another source's artifact. Source changes and generation changes miss naturally.

The cache is process-local, protected by an `RLock`, and uses an LRU limit of 128 entries. Source larger than 262,144 UTF-8 bytes bypasses it. Miss preparation is serialized to prevent a same-source compile stampede; runtime imports, `exec`, class construction, and component construction occur outside the lock. Failed validation/compilation is not inserted. `clear_component_compilation_cache()` clears artifacts, counters, and the process-level feature-setting snapshot.

The feature is disabled by default. Enable with:

```bash
LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED=true make backend
```

Disable or roll back with `LANGFLOW_COMPONENT_COMPILATION_CACHE_ENABLED=false` (the default). A running worker reads the setting once; restart it, or call the internal clear hook in tests/lifecycle code, after changing the environment.

## Results

Percentages are relative to the baseline; negative is faster/lower. Values are milliseconds unless noted.

### Repeated same-source evaluation

| Metric | Baseline | Cache OFF | OFF vs baseline | Cache ON | ON vs baseline |
|---|---:|---:|---:|---:|---:|
| Parse/compile activity p50 | 0.439 | 0.395 | -10.0% | 0.282 | -35.8% |
| Parse/compile activity p95 | 0.452 | 0.410 | -9.2% | 0.291 | -35.5% |
| Namespace/class creation p50 | 1.247 | 1.257 | +0.8% | 0.979 | -21.5% |
| Namespace/class creation p95 | 1.335 | 1.357 | +1.7% | 1.068 | -20.0% |
| Constructor p50 | 0.092 | 0.090 | -1.8% | 0.087 | -5.4% |
| Constructor p95 | 0.099 | 0.099 | +0.7% | 0.094 | -4.9% |

The parse/compile probe is deliberately labeled as broad activity: it includes runtime annotation work and excludes hashing, validation walks, serialization, and LRU overhead. The implementation-specific phase probe measured the cache-OFF artifact build at 0.324/0.343 ms p50/p95 (41 builds), the cache-ON first build at 0.323 ms, and cache-hit AST restoration at 0.077/0.091 ms p50/p95 (40 restores). Baseline has no separable artifact phase, so its phase-probe fields are zero rather than a misleading estimate.

The constructor is never skipped; its small apparent change is run-to-run noise rather than cached work. On the first enabled miss, broad parse/compile activity was 0.433 ms, class creation 1.378 ms, and construction 0.122 ms. The steady workload recorded 1 build, 1 miss, and 45 hits (including warm-up/probes), while all 40 measured evaluations had distinct class identities.

Different-source class creation was 1.205 ms p50 with cache ON. Updating source in the same worker took 1.116 ms and produced a miss/new artifact.

### Graph preparation

| Workload | Percentile | Baseline | Cache OFF | OFF vs baseline | Cache ON | ON vs baseline |
|---|---|---:|---:|---:|---:|---:|
| Cold 10-node preparation | p50 | 52.196 | 56.290 | +7.8% | 49.971 | -4.3% |
| Cold 10-node preparation | p95 | 52.729 | 63.339 | +20.1% | 51.256 | -2.8% |
| Warm 10-node preparation | p50 | 13.644 | 13.866 | +1.6% | 11.509 | -15.7% |
| Warm 10-node preparation | p95 | 13.748 | 14.617 | +6.3% | 11.724 | -14.7% |
| Cold 100-node preparation | p50 | 526.278 | 538.516 | +2.3% | 497.054 | -5.6% |
| Cold 100-node preparation | p95 | 536.193 | 551.765 | +2.9% | 503.536 | -6.1% |
| Warm 100-node preparation | p50 | 134.596 | 140.156 | +4.1% | 113.938 | -15.3% |
| Warm 100-node preparation | p95 | 143.670 | 154.756 | +7.7% | 115.072 | -19.9% |

### Flow wall time

| Workload | Percentile | Baseline | Cache OFF | OFF vs baseline | Cache ON | ON vs baseline |
|---|---|---:|---:|---:|---:|---:|
| Cold 10-node flow | p50 | 54.928 | 59.769 | +8.8% | 52.653 | -4.1% |
| Cold 10-node flow | p95 | 55.631 | 66.751 | +20.0% | 54.006 | -2.9% |
| Warm 10-node flow | p50 | 16.359 | 16.750 | +2.4% | 14.135 | -13.6% |
| Warm 10-node flow | p95 | 16.586 | 17.473 | +5.3% | 14.477 | -12.7% |
| Cold 100-node flow | p50 | 580.677 | 595.027 | +2.5% | 551.222 | -5.1% |
| Cold 100-node flow | p95 | 592.103 | 609.806 | +3.0% | 557.627 | -5.8% |
| Warm 100-node flow | p50 | 188.281 | 194.940 | +3.5% | 168.124 | -10.7% |
| Warm 100-node flow | p95 | 198.989 | 213.679 | +7.4% | 172.828 | -13.1% |

The enabled 10-node graph workload recorded 1 build, 1 miss, and 319 hits; the 100-node workload recorded 1 build, 1 miss, and 3,199 hits. Warm graph copies still instantiate every component for every run.

### Concurrency, compile probes, and CPU profile

| Metric | Baseline | Cache OFF | OFF vs baseline | Cache ON | ON vs baseline |
|---|---:|---:|---:|---:|---:|
| 32 same-source requests wall time | 38.822 | 38.969 | +0.4% | 32.139 | -17.2% |
| 32 distinct-source requests p50 | 38.529 | 38.250 | -0.7% | 38.765 | +0.6% |
| 32 distinct-source requests p95 | 40.202 | 39.305 | -2.2% | 42.550 | +5.8% |
| Parse/compile probe calls, 40 steady evaluations | 4,880 | 4,840 | -0.8% | 4,760 | -2.5% |

The same-source concurrent cache-ON run had 1 build, 1 miss, 31 hits, and 32 distinct class identities. Each distinct-source batch had 32 builds/misses; the global miss lock increased p95 by 5.8% while p50 was effectively neutral. The broad probe count includes runtime annotation resolution that deliberately remains per request; the source-artifact build count fell to one only for the repeated-source workload. The cache-ON profile still attributes most time to `prepare_global_scope` and runtime annotation snapshots, explaining why cache hits do not eliminate most component-creation cost.

### Memory

| Metric for 128 unique sources | Baseline | Cache OFF | Cache ON | ON vs baseline |
|---|---:|---:|---:|---:|
| RSS delta | 901,120 B | 868,352 B | 2,392,064 B | +165.5% |
| Traced allocation delta | 1,323,586 B | 987,930 B | 2,370,081 B | +79.1% |

The enabled traced delta is about 18.1 KiB per full cache entry at the 128-entry limit. RSS is allocator- and process-state-sensitive, so this is a bounded workload estimate, not a production sizing guarantee.

## Assessment

The cache improved repeated-source workloads in this run: 100-node warm flow improved 10.7% at p50 and 13.1% at p95, and same-source concurrency improved 17.2%. It did not improve the miss-heavy distinct-source tail: p95 regressed 5.8%, quantifying the global miss-lock risk. Cache OFF also showed up to 20% variation in the very short 10-node p95 result, so small-run differences should be treated as noise/regression signals to monitor rather than evidence from a single run.

Recommendation: keep the default off and enable only as an opt-in canary for deployments with repeated identical component source and meaningful warm-graph reuse. Monitor worker RSS, hit rate, and small-flow tail latency before wider rollout. Remaining risks are workload-dependent hit rate, serialized miss preparation for many concurrent unique sources, per-process memory multiplication across workers, and invalidation discipline when source-derived validation/compiler rules change; such changes must increment `COMPONENT_COMPILATION_ARTIFACT_GENERATION`.
