#!/usr/bin/env bash
set -euo pipefail

# Minimal oracle solution: read first dataset from source_data.json, generate a basic chart
python3 - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

APP_ROOT = Path(os.environ.get("OPENSKILLEVAL_APP_ROOT", "/app"))
DATA_FILE = APP_ROOT / "benchmark" / "source_data.json"
TASK_FILE = APP_ROOT / "benchmark" / "task_input.json"
OUTPUT_DIR = APP_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = "result.png"
DPI = 150
FIGSIZE = (12, 8)

data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
task = json.loads(TASK_FILE.read_text(encoding="utf-8"))

goal = task.get("goal", {})
insight = goal.get("insight", "Data Visualization")

# Pick the first dataset that has a "data" array
ds_key = None
dataset = None
for key, val in data.items():
    if isinstance(val, dict) and "data" in val and isinstance(val["data"], list):
        ds_key = key
        dataset = val
        break

if dataset and dataset["data"]:
    ds_data = dataset["data"]
    unit = dataset.get("unit", "")
    description = dataset.get("description", "")

    if isinstance(ds_data[0], dict):
        keys = list(ds_data[0].keys())

        if len(keys) == 2:
            # Simple x-y data → line chart
            x_key, y_key = keys[0], keys[1]
            xs = [d[x_key] for d in ds_data]
            ys = [d[y_key] for d in ds_data]

            fig, ax = plt.subplots(figsize=FIGSIZE)
            ax.plot(xs, ys, "o-", color="#d62728", linewidth=2, markersize=4)

            if all(isinstance(x, (int, float)) for x in xs):
                z = np.polyfit(xs, ys, 2)
                p = np.poly1d(z)
                ax.plot(xs, p(xs), "--", color="#ff7f0e", linewidth=1.5,
                        label="Trend")
                ax.legend()

            ax.set_title(insight[:80], fontsize=13)
            ax.set_xlabel(x_key.replace("_", " ").title())
            ylabel = f"{y_key.replace('_', ' ').title()} ({unit})" if unit else y_key
            ax.set_ylabel(ylabel)
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            fig.savefig(OUTPUT_DIR / OUTPUT_FILE, dpi=DPI)
            plt.close(fig)

        else:
            # Multi-column data → multi-line chart
            x_key = keys[0]
            value_keys = keys[1:]
            xs = [d[x_key] for d in ds_data]

            fig, ax = plt.subplots(figsize=FIGSIZE)
            for vk in value_keys:
                vals = [d.get(vk, 0) for d in ds_data]
                ax.plot(xs, vals, "o-", linewidth=2, markersize=4, label=vk)

            ax.set_title(insight[:80], fontsize=13)
            ax.set_xlabel(x_key.replace("_", " ").title())
            ax.set_ylabel(unit or "Value")
            ax.legend()
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            fig.savefig(OUTPUT_DIR / OUTPUT_FILE, dpi=DPI)
            plt.close(fig)
    else:
        fig, ax = plt.subplots(figsize=FIGSIZE)
        ax.text(0.5, 0.5, insight[:200], ha="center", va="center",
                fontsize=12, transform=ax.transAxes, wrap=True)
        ax.set_title(description or "Visualization")
        fig.savefig(OUTPUT_DIR / OUTPUT_FILE, dpi=DPI)
        plt.close(fig)
else:
    fig, ax = plt.subplots(figsize=FIGSIZE)
    ax.text(0.5, 0.5, insight[:200], ha="center", va="center",
            fontsize=12, transform=ax.transAxes, wrap=True)
    ax.set_title("Data Visualization")
    fig.savefig(OUTPUT_DIR / OUTPUT_FILE, dpi=DPI)
    plt.close(fig)

print(f"[solve] saved {OUTPUT_FILE} to {OUTPUT_DIR}")
PY
