#!/usr/bin/env python3
"""Launch the repository's audited Starter Kit without copying secrets."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "starter-kit" / "python" / "run_local.py"


if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable, str(LAUNCHER), *sys.argv[1:]])
