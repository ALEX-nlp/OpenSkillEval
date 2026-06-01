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
    unzip \
    zip \
    curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz | \
    tar -xJ -C /usr/local --strip-components=1

RUN pip install playwright==1.50.0 && \
    playwright install --with-deps chromium && \
    rm -f /usr/local/bin/playwright && \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g playwright@1.50.0

ENV NODE_PATH=/usr/local/lib/node_modules

RUN mkdir -p /app/libs /app/libs/fonts && \
    curl -sL -o /app/libs/chart.js            "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js" && \
    curl -sL -o /app/libs/d3.min.js           "https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js" && \
    curl -sL -o /app/libs/html-to-image.js    "https://cdn.jsdelivr.net/npm/html-to-image@1.11.11/dist/html-to-image.js" && \
    curl -sL -o /app/libs/mermaid.min.js      "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js" && \
    curl -sL -o /app/libs/mermaid.esm.min.mjs "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs" && \
    curl -sL -o /app/libs/motion.js           "https://cdn.jsdelivr.net/npm/motion@12/dist/motion.js" && \
    curl -sL -o /app/libs/tailwindcss.js      "https://cdn.tailwindcss.com" && \
    curl -sL -o /app/libs/three.module.min.js "https://cdn.jsdelivr.net/npm/three@0.170/build/three.module.min.js" && \
    curl -sL -o /app/libs/reveal.css          "https://cdn.jsdelivr.net/npm/reveal.js@5/dist/reveal.css" && \
    curl -sL -o /app/libs/reveal-theme-white.css "https://cdn.jsdelivr.net/npm/reveal.js@5/dist/theme/white.css" && \
    curl -sL -o /app/libs/reveal.js           "https://cdn.jsdelivr.net/npm/reveal.js@5/dist/reveal.js" && \
    curl -sL -o /app/libs/leaflet.css         "https://cdn.jsdelivr.net/npm/leaflet@1/dist/leaflet.css" && \
    curl -sL -o /app/libs/leaflet.js          "https://cdn.jsdelivr.net/npm/leaflet@1/dist/leaflet.js" && \
    curl -sL -o /app/libs/fonts/inter.css     "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" && \
    curl -sL -o /app/libs/fonts/noto-sans-sc.css "https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;600;700;800;900&display=swap"

WORKDIR /app

RUN pip install \
    pillow==11.1.0 \
    pytest==8.3.5

RUN mkdir -p /app/output

COPY skills /app/skills

# Benchmark assets (mounted by harbor at run time)
COPY benchmark /app/benchmark
