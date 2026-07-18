#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$BASE_DIR/app/generated"
BAD_OUT_DIR="$BASE_DIR/app/generated"$'\r'

rm -rf "$OUT_DIR" "$BAD_OUT_DIR"
mkdir -p "$OUT_DIR"

python -m grpc_tools.protoc \
  -I "$BASE_DIR/proto" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$BASE_DIR/proto/task_scheduler.proto" \
  "$BASE_DIR/proto/agent_orchestrator.proto"

touch "$OUT_DIR/__init__.py"

echo "[OK] generated proto files into $OUT_DIR"
find "$OUT_DIR" -name "*pb2*.py" -print

test -f "$OUT_DIR/task_scheduler_pb2.py"
test -f "$OUT_DIR/task_scheduler_pb2_grpc.py"
