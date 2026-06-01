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

# ── Python packages for trajectory analysis and optional re-execution ──
RUN pip install \
    # Data manipulation
    pandas==2.2.3 \
    numpy==1.26.4 \
    # Scientific computing
    scipy==1.14.1 \
    # Visualization (needed to safely re-execute data loading code from trajectories)
    matplotlib==3.9.3 \
    seaborn==0.13.2 \
    plotly==5.24.1 \
    # JSON / YAML
    pyyaml==6.0.2 \
    # Pattern matching
    regex==2024.11.6 \
    # Testing
    pytest==8.3.5

WORKDIR /app

RUN mkdir -p /app/output /app/eval_output

COPY benchmark /app/benchmark
