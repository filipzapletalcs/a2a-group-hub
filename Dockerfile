# Multi-stage Dockerfile for A2A Group Chat Hub
# Supports running the hub server or individual agents

FROM python:3.12-slim AS base

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[all]"

# Copy source
COPY src/ src/
COPY agents/ agents/
COPY tests/ tests/

# Default: run the hub
ENV STORAGE_BACKEND=memory
ENV HUB_HOST=0.0.0.0
ENV HUB_PORT=8000

EXPOSE 8000

# Hub server
CMD ["python", "-m", "uvicorn", "src.hub.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
