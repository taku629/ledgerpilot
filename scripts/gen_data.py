#!/usr/bin/env python3
"""Regenerate the synthetic demo dataset (deterministic, seeded)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data_gen import generate  # noqa: E402

if __name__ == "__main__":
    out = generate()
    for k, v in out.items():
        print(f"{k}: {v}")
