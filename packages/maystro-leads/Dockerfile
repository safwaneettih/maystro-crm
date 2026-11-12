FROM python:3.12-slim-bookworm

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LEADS_DATA_DIR=/data/leads \
    TZ=Africa/Tunis

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 tzdata \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home collector \
    && install -d -o collector -g collector "$LEADS_DATA_DIR"

COPY leads_daily.py schedule.py twenty_import.py ./
USER collector
CMD ["python", "schedule.py"]
