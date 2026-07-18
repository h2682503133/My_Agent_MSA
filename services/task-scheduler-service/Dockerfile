FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

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
    find /service/app/generated -name "*pb2*.py" -print && \
    test -f /service/app/generated/task_scheduler_pb2.py && \
    test -f /service/app/generated/task_scheduler_pb2_grpc.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/service:/service/app/generated
ENV SCHEDULER_HOST=0.0.0.0
ENV SCHEDULER_GRPC_PORT=5100
ENV SCHEDULER_PORT=5100
ENV ORCHESTRATOR_TARGET=agent-orchestrator-service:5300

ENV TZ=Asia/Shanghai

EXPOSE 5100
CMD ["python", "-m", "app.main"]
