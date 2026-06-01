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
    curl \
    ca-certificates \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz | tar -xJ -C /usr/local --strip-components=1

WORKDIR /tmp/tpl

RUN npx --yes create-next-app@latest nextjs-app \
    --typescript --app --eslint --no-tailwind --no-src-dir \
    --import-alias "@/*" --use-npm

WORKDIR /tmp/tpl/nextjs-app

RUN npm install -D tailwindcss @tailwindcss/postcss

RUN npm install remixicon @remixicon/react

RUN mkdir -p /app/templates && mv /tmp/tpl/nextjs-app /app/templates/nextjs-app

RUN rm -rf /tmp/tpl

RUN npm install -g turbo pnpm yarn

RUN pip install playwright==1.50.0 && \
    playwright install --with-deps chromium && \
    rm -f /usr/local/bin/playwright && \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g playwright@1.50.0

ENV NODE_PATH=/usr/local/lib/node_modules

WORKDIR /app

RUN pip install \
    pytest==8.3.5 \
    beautifulsoup4==4.12.3 \
    lxml==5.3.0

RUN mkdir -p /app/output

COPY skills /app/skills

RUN chmod +x /app/skills/scripts/*.py 2>/dev/null || true

# Benchmark assets (mounted by harbor at run time)
COPY benchmark /app/benchmark
