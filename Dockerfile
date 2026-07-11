FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY handler ./handler
COPY dispatcher ./dispatcher
COPY worker ./worker
COPY reaper ./reaper
COPY migrations ./migrations

# Default to the worker; compose overrides `command` per service.
CMD ["python", "-m", "worker"]
