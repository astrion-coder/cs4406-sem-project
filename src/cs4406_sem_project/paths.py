"""Filesystem layout. Everything large lives under ``data/`` inside the repo (gitignored),
because the home partition is too small for MS MARCO."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("CS4406_DATA_DIR", REPO_ROOT / "data"))
INDEX_DIR = DATA_DIR / "indexes"
ENCODED_DIR = DATA_DIR / "encoded"
RESULTS_DIR = REPO_ROOT / "results"

# ir_datasets defaults to ~/.ir_datasets; redirect it before ir_datasets is imported anywhere.
os.environ.setdefault("IR_DATASETS_HOME", str(DATA_DIR / "ir_datasets"))


def index_path(name: str) -> Path:
    return INDEX_DIR / name
