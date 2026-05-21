"""Entry-point wrapper around tender_monitor.scheduler.seed.main.

Usable as ``python scripts/seed_sources.py [path]``; the CLI's
``tender-monitor seed-sources`` calls the same underlying function.
"""

from __future__ import annotations

import sys

from tender_monitor.scheduler.seed import DEFAULT_PATH, main

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_PATH)
    sys.exit(main(path))
