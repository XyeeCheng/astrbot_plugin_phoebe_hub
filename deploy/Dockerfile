FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY dsh_bridge/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home phoebe && mkdir -p /state /home/phoebe/.cache && chown phoebe /state /home/phoebe/.cache
COPY dsh_bridge /app/dsh_bridge
USER 10001:10001
CMD ["python", "-m", "dsh_bridge.server"]
