FROM python:3.12-slim

LABEL maintainer="zonfacter"
LABEL description="Hytale Server Dashboard"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY worker.py .
COPY templates/ templates/
COPY static/ static/

# Create data directory for SQLite
RUN mkdir -p /app/data

# Environment variables with defaults
ENV DASH_USER=admin
ENV DASH_PASS=change-me
ENV ALLOW_CONTROL=true
ENV CF_API_KEY=""
ENV HYTALE_DOCKER_CONTAINER=""

# Expose port
EXPOSE 8088

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8088/metrics || exit 1

# Run with uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
