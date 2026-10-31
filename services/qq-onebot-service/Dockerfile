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
    test -f /service/proto_gen/task_scheduler_pb2_grpc.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/service:/service/proto_gen
ENV WORKSPACE_DIR=/app/workspace
ENV SCHEDULER_TARGET=task-scheduler-service.agent.svc.cluster.local:5100
ENV ONEBOT_MODE=forward
ENV ONEBOT_WS_URL=ws://host.docker.internal:3001/
ENV ONEBOT_WS_LISTEN=0.0.0.0:6700
ENV ONEBOT_TOKEN=""
ENV SUBSCRIBER_ID=qq-onebot-1
ENV SUBSCRIBER_PRIORITY=90

CMD ["python", "-m", "app.main"]
