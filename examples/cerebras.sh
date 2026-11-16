#!/usr/bin/env bash
# Runs agent-trials with opencode against a model hosted on Cerebras.
# Usage: CEREBRAS_API_KEY=... examples/cerebras.sh [model] [runs]
#   model: a model id from https://api.cerebras.ai/v1/models, default gpt-oss-120b
#   runs:  default 3
#
# opencode needs --standalone here: its shared background service does not
# inherit CEREBRAS_API_KEY from this shell and reports the model as unavailable.
set -euo pipefail

if [ -z "${CEREBRAS_API_KEY:-}" ]; then
  echo "set CEREBRAS_API_KEY first" >&2
  exit 1
fi

model="${1:-gpt-oss-120b}"
runs="${2:-3}"
here="$(cd "$(dirname "$0")" && pwd)"

exec "$here/../agent-trials.sh" "opencode-cerebras-$model" "$runs" \
  opencode run --standalone --auto -m "cerebras/$model"
