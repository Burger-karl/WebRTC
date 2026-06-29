# ─────────────────────────────────────────────────────────────────────────────
# MeetFree — Dockerfile
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
# curl  — used by the healthcheck (curl -f http://localhost:5000/health)
# default-libmysqlclient-dev — needed by some PyMySQL builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    default-libmysqlclient-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (Docker cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/  ./backend/
COPY frontend/ ./frontend/

# Add backend to PYTHONPATH so imports like `from config import cfg` work
# when gunicorn runs with module path backend.server:app
ENV PYTHONPATH=/app/backend

# Run as non-root for security
RUN useradd -m -u 1000 meetfree \
    && chown -R meetfree:meetfree /app
USER meetfree

EXPOSE 5000

# Default CMD (overridden by docker-compose command:)
CMD ["gunicorn", \
     "--worker-class", "eventlet", \
     "--workers", "1", \
     "--bind", "0.0.0.0:5000", \
     "--timeout", "120", \
     "--log-level", "info", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "backend.server:app"]