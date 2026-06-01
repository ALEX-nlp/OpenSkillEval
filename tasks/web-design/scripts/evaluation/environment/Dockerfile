FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Credentials (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL)
# are injected at runtime by the eval pipeline (from agent_configs/snippets/judge.snippet
# via docker exec -e). Do not hardcode keys here.
ENV API_TIMEOUT_MS=600000 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# Optional: point at a non-default endpoint (e.g. OpenRouter)
# ENV ANTHROPIC_BASE_URL=https://api.anthropic.com

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python-is-python3 \
    curl ca-certificates xz-utils \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 LTS (apt nodejs=18 has IPv6-first DNS resolution that
# misbehaves with bundled tooling inside this container)
RUN curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz | \
    tar -xJ -C /usr/local --strip-components=1

# Python deps: Playwright + evaluation tools
RUN pip install \
    playwright==1.50.0 \
    beautifulsoup4==4.12.3 \
    lxml==5.3.0 \
    pytest==8.3.5 \
    Pillow==11.1.0

# Install Chromium for Playwright
RUN playwright install --with-deps chromium

WORKDIR /app

# Copy benchmark data (task_input.json, source_brief.md, agent output)
# In practice, the agent output is mounted/copied at runtime
RUN mkdir -p /app/output /app/eval_output

COPY benchmark /app/benchmark
