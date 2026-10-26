FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
RUN bash scripts/gen_proto.sh

ENV EMBEDDING_GRPC_PORT=5303
ENV EMBEDDING_API_BASE=http://host.docker.internal:11434/v1/embeddings
ENV EMBEDDING_MODEL=qwen3-embedding:0.6b
ENV EMBEDDING_API_KEY=ollama
ENV EMBEDDING_TIMEOUT_SECONDS=30

ENV TZ=Asia/Shanghai

EXPOSE 5303

CMD ["python", "-m", "app.main"]
