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
    fontconfig \
    fonts-noto-cjk \
    unzip \
    zip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz | \
    tar -xJ -C /usr/local --strip-components=1

RUN npm install -g @antv/infographic

RUN mkdir -p /app/vendor && \
    ANTV_DIR=$(npm root -g)/@antv/infographic/dist && \
    if [ -f "$ANTV_DIR/infographic.min.js" ]; then \
      cp "$ANTV_DIR/infographic.min.js" /app/vendor/infographic.min.js; \
    elif [ -f "$ANTV_DIR/infographic.js" ]; then \
      cp "$ANTV_DIR/infographic.js" /app/vendor/infographic.min.js; \
    else \
      echo "WARNING: infographic JS bundle not found at $ANTV_DIR" && ls -la "$ANTV_DIR" 2>/dev/null || true; \
    fi

RUN pip install playwright==1.50.0 && \
    playwright install --with-deps chromium && \
    rm -f /usr/local/bin/playwright && \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g playwright@1.50.0

ENV NODE_PATH=/usr/local/lib/node_modules

WORKDIR /app

RUN pip install \
    pillow==11.1.0 \
    pytest==8.3.5

RUN mkdir -p /app/output

COPY skills /app/skills

# Benchmark assets (mounted by harbor at run time)
COPY benchmark /app/benchmark
