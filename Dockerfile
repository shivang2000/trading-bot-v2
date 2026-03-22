FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency file first for layer caching
COPY pyproject.toml .

# Create a minimal package so pip install works
RUN mkdir -p src && touch src/__init__.py

# Install Python dependencies
RUN pip install --no-cache-dir .

# Copy source code
COPY . .

# Re-install to register the package properly
RUN pip install --no-cache-dir -e .

# Create non-root user
RUN useradd --create-home --shell /bin/bash botuser \
    && chown -R botuser:botuser /app

USER botuser

# Ensure data and logs dirs exist
RUN mkdir -p data logs

CMD ["python", "-m", "src.main"]
