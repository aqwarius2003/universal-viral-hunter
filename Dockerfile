# Multi-stage / lightweight Python 3.11 production container
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VERSION=2.4.3 \
    POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_CREATE=false \
    DB_PATH="/app/data/tweets.sqlite"

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install poetry
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="${POETRY_HOME}/bin:${PATH}"

# Set workdir
WORKDIR /app

# Copy dependency files first for caching
COPY pyproject.toml poetry.lock* /app/

# Install python dependencies (without dev) and playwright chromium, then clean apt cache
RUN poetry install --no-root --without dev --no-interaction --no-ansi \
    && playwright install --with-deps chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /root/.cache

# Copy application source code
COPY . /app/

# Create data directory for SQLite database persistence
RUN mkdir -p /app/data

# Default entrypoint: Periodic scheduler
CMD ["python", "-m", "src.scheduler"]
