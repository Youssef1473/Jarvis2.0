"""Package-level entry point for Jarvis 2.0 enabling `python -m jarvis` execution."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from run import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
