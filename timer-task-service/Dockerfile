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
    find /service/app/generated -name "*pb2*.py" -print && \
    test -f /service/app/generated/timer_task_pb2.py && \
    test -f /service/app/generated/timer_task_pb2_grpc.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/service:/service/app/generated
ENV TIMER_GRPC_HOST=0.0.0.0
ENV TIMER_GRPC_PORT=5103
ENV SCHEDULER_TARGET=task-scheduler-service.agent.svc.cluster.local:5100

EXPOSE 5103
CMD ["python", "-m", "app.main"]
