FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# FIX: Add /app/backend to PYTHONPATH so that `from config import cfg`
# and `from auth import ...` resolve correctly when Gunicorn runs
# from /app using the module path backend.server:app
ENV PYTHONPATH=/app/backend

# Non-root user for security
RUN useradd -m -u 1000 meetfree && chown -R meetfree:meetfree /app
USER meetfree

EXPOSE 5000

CMD ["gunicorn", "--worker-class", "eventlet", "--workers", "1", \
     "--bind", "0.0.0.0:5000", "--timeout", "120", \
     "backend.server:app"]