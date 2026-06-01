FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python-is-python3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    libcairo2 \
    ripgrep \
    curl ca-certificates xz-utils \
    unzip \
    zip \
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
    jinja2==3.1.5 \
    pandas==2.2.3 \
    click==8.1.7 \
    weasyprint==63.1 \
    matplotlib==3.9.3 \
    pillow==11.1.0 \
    jsonschema==4.23.0 \
    pytest==8.3.5

RUN mkdir -p /app/output

COPY skills /app/skills

# Benchmark assets (mounted by harbor at run time)
COPY benchmark /app/benchmark
