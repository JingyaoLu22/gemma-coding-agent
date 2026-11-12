#!/usr/bin/env bash
# Install the optional ADK/SWE compiler from explicitly supplied local wheels.
set -euo pipefail
cd "$(dirname "$0")/.."
WHEEL_DIR="${1:-.cache/runtime-wheels}"
WHEELS=(
  adk_eval_core-0.1.0-py3-none-any.whl
  adk_submission-0.2.13-py3-none-any.whl
  google_adk-1.36.1-py3-none-any.whl
  swegemma-0.2.11-py3-none-any.whl
)
for wheel in "${WHEELS[@]}"; do
  if [[ ! -f "$WHEEL_DIR/$wheel" ]]; then
    echo "Missing runtime wheel: $WHEEL_DIR/$wheel" >&2
    exit 1
  fi
done
if [[ ! -x .venv-harness/bin/python ]]; then
  uv venv -q -p 3.12 .venv-harness
fi
VIRTUAL_ENV=.venv-harness uv pip install "${WHEELS[@]/#/$WHEEL_DIR/}"
.venv-harness/bin/python -c "import adk_submission, swegemma; print('runtime compiler ready')"
