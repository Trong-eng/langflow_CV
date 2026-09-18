#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
baseline_sha="c9fbb3ef72c2027ce4fefd1f45d040ce6469a99d" # pragma: allowlist secret
archive_dir="$(mktemp -d "${TMPDIR:-/tmp}/langflow-component-cache-baseline.XXXXXX")"
output_path="${1:-$repo_root/benchmark_results/component_compilation_cache/baseline.json}"

cleanup() {
  rm -rf -- "$archive_dir"
}
trap cleanup EXIT

git -C "$repo_root" archive "$baseline_sha" | tar -x -C "$archive_dir"
mkdir -p "$archive_dir/scripts/benchmarks"
cp "$repo_root/scripts/benchmarks/benchmark_component_compilation_cache.py" \
  "$archive_dir/scripts/benchmarks/benchmark_component_compilation_cache.py"
printf '%s\n' "$baseline_sha" > "$archive_dir/.benchmark-source-revision"

cd "$repo_root/src/lfx"
PYTHONPATH="$archive_dir/src/lfx/src:$archive_dir/src/sdk/src" \
  uv run --no-sync python \
  "$archive_dir/scripts/benchmarks/benchmark_component_compilation_cache.py" \
  --mode baseline \
  --output "$output_path"
