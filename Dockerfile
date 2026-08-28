FROM python:3.11-slim

LABEL org.opencontainers.image.title="CUKTECH BLE Server Lite"
LABEL org.opencontainers.image.description="Minimal CUKTECH 10 GaN Charger BLE to MQTT server"

RUN apt-get update \
    && apt-get install -y --no-install-recommends bluez dbus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir .

COPY config.yaml.example ./config.yaml
RUN mkdir -p /data

ENV CUKTECH_CONFIG_PATH=/data/config.yaml
ENTRYPOINT ["python", "-m", "cuktech_server"]
