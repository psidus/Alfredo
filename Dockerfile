FROM python:3.10-slim
WORKDIR /app

# Install system dependencies including Xvfb for headless screenshots
RUN apt-get update && apt-get install -y \
    xvfb \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxrandr2 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxi6 \
    libxtst6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# The entrypoint will start Xvfb before running the requested command
ENTRYPOINT ["docker-entrypoint.sh"]
