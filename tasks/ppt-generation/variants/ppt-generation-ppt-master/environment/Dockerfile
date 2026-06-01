FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_TIMEOUT_MS=600000

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python-is-python3 \
    libreoffice \
    poppler-utils \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libpango-1.0-0 \
    pandoc \
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
    cairosvg==2.7.1 \
    google-genai==1.2.0 \
    numpy==2.1.3 \
    openai==1.68.2 \
    pillow==11.1.0 \
    PyMuPDF==1.24.10 \
    pytest==8.3.5 \
    python-pptx==1.0.2 \
    reportlab==4.2.5 \
    requests==2.32.3 \
    beautifulsoup4 \
    svglib==1.5.1

RUN mkdir -p /app/output

COPY skills /app/skills

# Benchmark assets (mounted by harbor at run time)
COPY benchmark /app/benchmark
