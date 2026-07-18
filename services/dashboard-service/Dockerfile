FROM python:3.11-slim

# 安装 kubectl
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/app/
COPY static/ /app/static/

ENV PYTHONUNBUFFERED=1
ENV CONFIG_ROOT=/app/config
ENV NAMESPACE=agent

EXPOSE 5600

CMD ["python", "-m", "app.main"]
