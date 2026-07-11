FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
RUN bash scripts/gen_proto.sh

ENV MODEL_PROXY_GRPC_PORT=5302
ENV MODEL_LIST_PATH=/app/config/model_list.json
ENV MODEL_PROFILES_PATH=/app/config/model_profiles.json
ENV REQUEST_TIMEOUT_SECONDS=300

ENV TZ=Asia/Shanghai

EXPOSE 5302

CMD ["python", "-m", "app.main"]
