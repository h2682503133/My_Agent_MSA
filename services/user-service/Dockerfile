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
    test -f /service/app/generated/user_pb2.py && \
    test -f /service/app/generated/user_pb2_grpc.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/service:/service/app/generated
ENV USER_GRPC_HOST=0.0.0.0
ENV USER_GRPC_PORT=5104
ENV USER_HTTP_PORT=5204
ENV USER_DATA_DIR=/data/users

ENV TZ=Asia/Shanghai

EXPOSE 5104
EXPOSE 5204
CMD ["python", "-m", "app.main"]
