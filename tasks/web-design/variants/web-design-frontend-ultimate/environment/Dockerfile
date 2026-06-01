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

RUN npx --yes create-vite@latest vite-react -- --template react-ts

WORKDIR /tmp/tpl/vite-react

RUN npm install && \
    npm install -D tailwindcss @tailwindcss/vite

RUN npm install framer-motion

RUN npm install tailwindcss-animate class-variance-authority clsx tailwind-merge && \
    npm install lucide-react @radix-ui/react-slot

RUN echo '{ \
  "$schema": "https://ui.shadcn.com/schema.json", \
  "style": "default", \
  "rsc": false, \
  "tsx": true, \
  "tailwind": { "css": "src/index.css", "baseColor": "slate", "cssVariables": true, "prefix": "" }, \
  "aliases": { "components": "@/components", "utils": "@/lib/utils", "ui": "@/components/ui", "lib": "@/lib", "hooks": "@/hooks" } \
}' > components.json

RUN npx --yes shadcn@latest add button badge card accordion dialog navigation-menu tabs sheet separator avatar alert -y || true

RUN mkdir -p src/lib && \
    echo 'import { type ClassValue, clsx } from "clsx"\nimport { twMerge } from "tailwind-merge"\n\nexport function cn(...inputs: ClassValue[]) {\n  return twMerge(clsx(inputs))\n}' > src/lib/utils.ts

RUN cd /tmp/tpl/vite-react && npm install -D parcel html-inline

RUN mkdir -p /app/templates && mv /tmp/tpl/vite-react /app/templates/vite-react

RUN rm -rf /tmp/tpl

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

RUN chmod +x /app/skills/frontend-design-ultimate/scripts/*.sh 2>/dev/null || true

# Benchmark assets (mounted by harbor at run time)
COPY benchmark /app/benchmark
