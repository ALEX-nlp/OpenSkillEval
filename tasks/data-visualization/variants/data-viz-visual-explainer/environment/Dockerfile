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
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz | \
    tar -xJ -C /usr/local --strip-components=1

RUN pip install playwright==1.50.0 && \
    playwright install --with-deps chromium && \
    rm -f /usr/local/bin/playwright && \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g playwright@1.50.0

ENV NODE_PATH=/usr/local/lib/node_modules

WORKDIR /app

RUN pip install \
    pandas==2.2.3 \
    pillow==11.1.0 \
    matplotlib==3.9.3 \
    pytest==8.3.5

RUN mkdir -p /app/libs && \
    curl -sL -o /app/libs/chart.js            "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js" && \
    curl -sL -o /app/libs/mermaid.esm.min.mjs "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs" && \
    curl -sL -o /app/libs/mermaid-layout-elk.esm.min.mjs "https://cdn.jsdelivr.net/npm/@mermaid-js/layout-elk/dist/mermaid-layout-elk.esm.min.mjs" && \
    curl -sL -o /app/libs/anime.min.js        "https://cdn.jsdelivr.net/npm/animejs@3.2.2/lib/anime.min.js"

RUN mkdir -p /app/output

COPY skills /app/skills

# Benchmark assets (mounted by harbor at run time)
COPY benchmark /app/benchmark
