FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client netcat-openbsd tzdata \
 && rm -rf /var/lib/apt/lists/*
ENV TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY labslots ./labslots
RUN pip install --no-cache-dir .
RUN useradd --system --uid 10001 --home /data labslots && mkdir -p /data && chown labslots /data
USER labslots
EXPOSE 3100
CMD ["uvicorn", "labslots.main:app", "--host", "0.0.0.0", "--port", "3100", "--proxy-headers", "--forwarded-allow-ips", "*"]
