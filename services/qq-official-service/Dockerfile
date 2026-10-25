FROM python:3.11-slim

WORKDIR /service

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN find /service -type f \( \
      -name "*.sh" -o \
      -name "*.proto" -o \
      -name "*.py" -o \
      -name "Dockerfile" -o \
      -name "requirements.txt" \
    \) -exec sed -i 's/\r$//' {} \; && \
    chmod +x /service/scripts/gen_proto.sh && \
    bash /service/scripts/gen_proto.sh && \
    test -f /service/proto_gen/task_scheduler_pb2.py && \
    test -f /service/proto_gen/user_pb2.py && \
    test -f /service/proto_gen/model_proxy_pb2.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/service:/service/proto_gen
ENV QQ_OFFICIAL_CONFIG_PATH=/service/config/qq_official_config.json
ENV SCHEDULER_TARGET=task-scheduler-service.agent.svc.cluster.local:5100
ENV USER_SERVICE_TARGET=user-service.agent.svc.cluster.local:5104
ENV MODEL_PROXY_TARGET=model-proxy-service:5302
ENV QQ_OFFICIAL_READER_PROFILE=reader
ENV SUBSCRIBER_ID=qq-official-1
ENV QQ_OFFICIAL_CHANNEL=qqofficial
ENV QQ_OFFICIAL_APP_ID=""
ENV QQ_OFFICIAL_CLIENT_SECRET=""

CMD ["python", "-m", "app.main"]
