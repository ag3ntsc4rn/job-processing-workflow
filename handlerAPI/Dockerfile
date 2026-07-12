# Image for the HTTP API. Separate from the base image so the FastAPI/uvicorn
# stack isn't pulled into the worker/dispatcher/reaper images. Build context is
# the repo root:  docker build -f handlerAPI/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements-api.txt

COPY common ./common
COPY handlerAPI ./handlerAPI

# Run as an unprivileged user.
RUN useradd -u 10001 -r -s /usr/sbin/nologin appuser
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz').status==200 else 1)"

CMD ["python", "-m", "handlerAPI"]
