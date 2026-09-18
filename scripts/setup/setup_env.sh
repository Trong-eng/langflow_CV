#!/bin/bash

# Create a .env if it doesn't exist, log all cases
if [ ! -f .env ]; then
  echo "Creating .env file"
  touch .env
fi

# Ensure workspace packages are reachable in .venv on macOS (avoids Python 3.13 skipping UF_HIDDEN .pth files)
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for p in "$ROOT_DIR"/.venv/lib/python3.*/site-packages; do
  if [ -d "$p" ]; then
    cat << EOF > "$p/langflow_dev.pth"
$ROOT_DIR/src/backend/base
$ROOT_DIR/src/lfx/src
$ROOT_DIR/src/sdk/src
$ROOT_DIR/src/bundles/amazon/src
$ROOT_DIR/src/bundles/anthropic/src
$ROOT_DIR/src/bundles/azure/src
$ROOT_DIR/src/bundles/cohere/src
$ROOT_DIR/src/bundles/datastax/src
$ROOT_DIR/src/bundles/docling/src
$ROOT_DIR/src/bundles/google/src
$ROOT_DIR/src/bundles/ibm/src
$ROOT_DIR/src/bundles/ollama/src
$ROOT_DIR/src/bundles/openai/src
$ROOT_DIR/src/bundles/openai-compatible/src
$ROOT_DIR/src/bundles/oracle/src
$ROOT_DIR/src/bundles/toolguard/src
$ROOT_DIR/src/bundles/vllm/src
EOF
  fi
done

