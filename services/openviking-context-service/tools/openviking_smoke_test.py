"""
简单 gRPC 测试脚本。

使用：
  python tools/openviking_smoke_test.py

环境变量：
  OPENVIKING_TARGET=localhost:5301
"""

import os
import sys
from pathlib import Path

import grpc

GENERATED_DIR = Path(__file__).resolve().parent.parent / "app" / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

import openviking_context_pb2
import openviking_context_pb2_grpc

target = os.getenv("OPENVIKING_TARGET", "localhost:5301")

with grpc.insecure_channel(target) as channel:
    stub = openviking_context_pb2_grpc.OpenVikingContextStub(channel)

    append_resp = stub.AppendTurn(openviking_context_pb2.AppendTurnRequest(
        user_id="dujiawei",
        session_id="web_dujiawei",
        agent_id="main",
        task_id="test-task",
        user_message="你好",
        assistant_message="你好，这是一次 OpenViking 上下文服务测试。",
        commit_limit=0,
    ))
    print("AppendTurn:", append_resp)

    search_resp = stub.SearchContext(openviking_context_pb2.SearchContextRequest(
        user_id="dujiawei",
        session_id="web_dujiawei",
        agent_id="main",
        query="你好",
        max_messages=6,
        commit_limit=0,
    ))
    print("SearchContext:", search_resp)
