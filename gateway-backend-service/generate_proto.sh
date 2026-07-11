#!/usr/bin/env sh
set -e

python -m grpc_tools.protoc \
  -I proto \
  --python_out=proto_gen \
  --grpc_python_out=proto_gen \
  proto/scheduler.proto \
  proto/user.proto \
  proto/tool_runtime.proto

echo "[OK] generated proto files into proto_gen/"
