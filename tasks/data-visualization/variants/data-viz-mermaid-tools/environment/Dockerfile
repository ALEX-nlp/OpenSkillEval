FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_TIMEOUT_MS=600000

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python-is-python3 \
    ripgrep \
    curl ca-certificates xz-utils \
    imagemagick file \
    libnss3 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2t64 libxshmfence1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz | \
    tar -xJ -C /usr/local --strip-components=1

RUN npm install -g @mermaid-js/mermaid-cli

RUN echo '{"args":["--no-sandbox","--disable-setuid-sandbox"]}' > /root/.puppeteerrc.json && \
    REAL=$(which mmdc) && \
    mv "$REAL" "${REAL}.real" && \
    printf '#!/bin/sh\nexec "%s.real" -p /root/.puppeteerrc.json "$@"\n' "$REAL" > "$REAL" && \
    chmod +x "$REAL"

RUN pip install playwright==1.50.0 && \
    playwright install --with-deps chromium && \
    rm -f /usr/local/bin/playwright && \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g playwright@1.50.0

ENV NODE_PATH=/usr/local/lib/node_modules

WORKDIR /app

RUN pip install \
    pandas==2.2.3 \
    pillow==11.1.0 \
    pytest==8.3.5

RUN mkdir -p /app/output

COPY skills /app/skills

RUN chmod +x /app/skills/*/scripts/*.sh 2>/dev/null || true

# Benchmark assets (mounted by harbor at run time)
COPY benchmark /app/benchmark
