#!/usr/bin/env bash
set -e

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$BASE_DIR/app/generated"

mkdir -p "$OUT_DIR"

python -m grpc_tools.protoc \
  -I "$BASE_DIR/proto" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$BASE_DIR/proto/agent_orchestrator.proto" \
  "$BASE_DIR/proto/model_proxy.proto" \
  "$BASE_DIR/proto/openviking_context.proto" \
  "$BASE_DIR/proto/tool_runtime.proto" \
  "$BASE_DIR/proto/timer_task.proto"

touch "$OUT_DIR/__init__.py"

echo "generated proto files into $OUT_DIR"
