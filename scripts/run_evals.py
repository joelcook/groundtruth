#!/usr/bin/env python3
"""Run the Golden Set. Thin wrapper around groundtruth.evals.

    python scripts/run_evals.py --tier fast      # deterministic replay, no API key
    python scripts/run_evals.py --tier full      # live model + judge (needs ANTHROPIC_API_KEY)
    python scripts/run_evals.py --tier release   # full, whole set, 100% required
    python scripts/run_evals.py --only place-a-hold
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from groundtruth import evals  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(evals.main())
