FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory for billing state persistence
RUN mkdir -p /data/usage

EXPOSE {{SERVER_PORT}}

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:{{SERVER_PORT}}/health')"

# Run the MCP server (SSE transport by default)
CMD ["python", "server.py"]
