# ============================================================
# Dockerfile - Algo Trading Platform
# Python 3.11 slim image
# ============================================================

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies + Microsoft ODBC Driver 17 for SQL Server.
# Debian version is detected dynamically rather than hardcoded, since
# python:3.11-slim's underlying Debian release can change between tags.
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    gcc \
    g++ \
    unixodbc \
    unixodbc-dev \
    && curl -sSL -O https://packages.microsoft.com/config/debian/$(grep VERSION_ID /etc/os-release | cut -d'"' -f2 | cut -d'.' -f1)/packages-microsoft-prod.deb \
    && dpkg -i packages-microsoft-prod.deb \
    && rm packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql17 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for Docker layer caching)
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TZ=Asia/Kolkata

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "from core.database.db import get_signals; get_signals(days=1)" || exit 1

CMD ["python", "run_single_scan.py"]
