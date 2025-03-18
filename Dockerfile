FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    mongodb-clients \
    tar \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./
COPY build_info.txt ./
COPY .env ./

# Create directories for logs and backups
RUN mkdir -p logs backups

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Create a non-root user for running the application
RUN useradd -m gobot && \
    chown -R gobot:gobot /app

USER gobot

# Command to run the application
CMD ["python", "main.py", "--mode", "bot"]

# Health check
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import requests; response = requests.get('https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe'); exit(0 if response.status_code == 200 else 1)"
