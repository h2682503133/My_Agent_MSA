FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata openssh-client ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install \
    --no-cache-dir \
    --timeout 120 \
    --retries 10 \
    \
    -r /app/requirements.txt

COPY . /app

RUN find /app -type f \( \
      -name "*.sh" -o \
      -name "*.proto" -o \
      -name "*.py" -o \
      -name "Dockerfile" -o \
      -name "requirements.txt" \
    \) -exec sed -i 's/\r$//' {} \; && \
    chmod +x /app/scripts/gen_proto.sh && \
    bash /app/scripts/gen_proto.sh && \
    find /app/app/generated -name "*pb2*.py" -print && \
    test -f /app/app/generated/tool_runtime_pb2.py && \
    test -f /app/app/generated/tool_runtime_pb2_grpc.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app:/app/app/generated

ENV TOOL_RUNTIME_HOST=0.0.0.0
ENV TOOL_RUNTIME_PORT=5303
ENV WORKSPACE_DIR=/app/workspace
ENV ENABLE_SHELL_TOOLS=true

ENV TZ=Asia/Shanghai

EXPOSE 5303

CMD ["python", "-m", "app.main"]
