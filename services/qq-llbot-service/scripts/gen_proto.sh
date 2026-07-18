#!/usr/bin/env sh
set -e

mkdir -p proto_gen

python -m grpc_tools.protoc \
  -I proto \
  --python_out=proto_gen \
  --grpc_python_out=proto_gen \
  proto/task_scheduler.proto

echo "[OK] generated proto files into proto_gen/"
