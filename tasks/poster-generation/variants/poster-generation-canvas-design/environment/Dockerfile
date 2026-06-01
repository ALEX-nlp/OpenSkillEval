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
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libpango-1.0-0 \
    fontconfig \
    poppler-utils \
    unzip \
    zip \
    libnss3 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libasound2t64 libxshmfence1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz | \
    tar -xJ -C /usr/local --strip-components=1

RUN npm install -g puppeteer && \
    echo '{"args":["--no-sandbox","--disable-setuid-sandbox"]}' > /root/.puppeteerrc.json

RUN pip install playwright==1.50.0 && \
    playwright install --with-deps chromium && \
    rm -f /usr/local/bin/playwright && \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g playwright@1.50.0

ENV NODE_PATH=/usr/local/lib/node_modules

WORKDIR /app

RUN pip install \
    cairosvg==2.7.1 \
    pillow==11.1.0 \
    reportlab==4.2.5 \
    pytest==8.3.5

RUN mkdir -p /app/output

COPY skills /app/skills

RUN mkdir -p /usr/share/fonts/canvas-design && \
    cp /app/skills/canvas-design/canvas-fonts/*.ttf /usr/share/fonts/canvas-design/ 2>/dev/null || true && \
    fc-cache -f -v 2>/dev/null || true

# Benchmark assets (mounted by harbor at run time)
COPY benchmark /app/benchmark
