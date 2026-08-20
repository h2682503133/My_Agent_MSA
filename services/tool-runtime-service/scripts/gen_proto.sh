#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$BASE_DIR/app/generated"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

python -m grpc_tools.protoc \
  -I "$BASE_DIR/proto" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$BASE_DIR/proto/tool_runtime.proto" \
  "$BASE_DIR/proto/model_proxy.proto"

touch "$OUT_DIR/__init__.py"

echo "[OK] generated proto files into $OUT_DIR"
find "$OUT_DIR" -name "*pb2*.py" -print

test -f "$OUT_DIR/tool_runtime_pb2.py"
test -f "$OUT_DIR/tool_runtime_pb2_grpc.py"
test -f "$OUT_DIR/model_proxy_pb2.py"
test -f "$OUT_DIR/model_proxy_pb2_grpc.py"
