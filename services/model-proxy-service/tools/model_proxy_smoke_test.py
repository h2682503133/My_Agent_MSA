"""
简单 gRPC 测试脚本。

使用：
  python tools/model_proxy_smoke_test.py

环境变量：
  MODEL_PROXY_TARGET=localhost:5302
  MODEL_PROFILE=default-main
"""

import os
import sys
import uuid
from pathlib import Path

import grpc

GENERATED_DIR = Path(__file__).resolve().parent.parent / "app" / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

import model_proxy_pb2
import model_proxy_pb2_grpc

target = os.getenv("MODEL_PROXY_TARGET", "localhost:5302")
profile = os.getenv("MODEL_PROFILE", "default-main")

with grpc.insecure_channel(target) as channel:
    stub = model_proxy_pb2_grpc.ModelProxyStub(channel)

    resp = stub.ChatCompletion(model_proxy_pb2.ChatCompletionRequest(
        request_id=f"test-{uuid.uuid4().hex[:8]}",
        task_id="test-task",
        agent_id="main",
        model_profile=profile,
        messages=[
            model_proxy_pb2.Message(role="system", content="你是测试助手。"),
            model_proxy_pb2.Message(role="user", content="请回复一句：模型代理服务测试成功。"),
        ],
        params={"temperature": "0.1", "stream": "false"},
    ))

    print(resp)
