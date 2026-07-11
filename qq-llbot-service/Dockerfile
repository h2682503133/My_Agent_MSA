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
ENV QQ_LLBOT_CONFIG_PATH=/service/config/qq_llbot_config.json
ENV SCHEDULER_TARGET=task-scheduler-service.agent.svc.cluster.local:5100
ENV SATORI_HOST=qq-satori-adapter
ENV SATORI_PORT=5600
ENV SATORI_TOKEN=""
ENV SUBSCRIBER_ID=qq-llbot-1

CMD ["python", "-m", "app.main"]
