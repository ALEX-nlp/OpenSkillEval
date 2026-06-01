"""Download case data from the OpenSkillEval HuggingFace dataset.

Pulls the raw case directories from the `jhying/OpenSkillEval` dataset repo
on the Hugging Face Hub into `tasks/<family>/shared/cases/` under the project
root. The HF repo mirrors the on-disk `tasks/` subtree, so a plain snapshot
download recreates the same layout locally.

Usage:
    python scripts/download_cases.py                       # all 5 families
    python scripts/download_cases.py --family web-design   # one family only

The download is resume-friendly: re-running the script only fetches files
that are missing or have changed. Dataset license: CC-BY-NC-4.0.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

REPO_ID = "jhying/OpenSkillEval"
REPO_TYPE = "dataset"
FAMILIES = (
    "data-visualization",
    "poster-generation",
    "ppt-generation",
    "report-generation",
    "web-design",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download OpenSkillEval case data from the Hugging Face Hub.",
    )
    parser.add_argument(
        "--family",
        choices=FAMILIES,
        default=None,
        help="Restrict download to a single task family (default: all 5).",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Local destination root (default: <repo root>/tasks).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Default destination = <project root>/tasks, since the HF repo mirrors
    # that subtree (tasks/<family>/shared/cases/...).
    project_root = Path(__file__).resolve().parent.parent
    dest = (args.dest or (project_root / "tasks")).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    if args.family:
        patterns = [f"tasks/{args.family}/shared/cases/**"]
        scope = f"family '{args.family}'"
    else:
        patterns = [f"tasks/{f}/shared/cases/**" for f in FAMILIES]
        scope = f"all {len(FAMILIES)} families"

    print(f"[download_cases] repo:   {REPO_ID} ({REPO_TYPE})")
    print(f"[download_cases] scope:  {scope}")
    print(f"[download_cases] dest:   {dest}")
    print("[download_cases] starting snapshot_download (resume-friendly)...")

    # snapshot_download is idempotent: existing files are skipped or
    # re-validated against the remote hash, so this is safe to re-run.
    try:
        local_path = snapshot_download(
            repo_id=REPO_ID,
            repo_type=REPO_TYPE,
            local_dir=str(dest.parent),  # parent so 'tasks/' lands inside it
            allow_patterns=patterns,
        )
    except Exception as exc:
        print(f"[download_cases] ERROR: snapshot_download failed: {exc}", file=sys.stderr)
        print(
            "[download_cases] If the dataset is private, set HF_TOKEN or run "
            "`huggingface-cli login` first.",
            file=sys.stderr,
        )
        return 1

    # snapshot_download can silently swallow some failure modes (e.g. an
    # auth error that returns the local_dir without raising); verify case
    # directories actually exist for every family we asked for.
    families_to_check = [args.family] if args.family else list(FAMILIES)
    n_total = 0
    for fam in families_to_check:
        cases_dir = dest / fam / "shared" / "cases"
        n = sum(1 for p in cases_dir.glob("*") if p.is_dir()) if cases_dir.exists() else 0
        print(f"[download_cases]   {fam:<20}  {n} case dirs")
        n_total += n

    if n_total == 0:
        print(
            "[download_cases] ERROR: 0 case directories materialized. "
            "Most likely cause: the dataset is private and you are not "
            "authenticated — set HF_TOKEN or run `huggingface-cli login`, "
            "then re-run.",
            file=sys.stderr,
        )
        return 2

    print(f"[download_cases] done. {n_total} case dirs under: {local_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
