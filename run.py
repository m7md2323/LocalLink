"""
run.py

Dev entry point. The installed ``locallink`` command points at
``engine.bootstrap:run`` (which is where all the bootstrap logic lives).
This file is a thin wrapper for contributors who prefer running from
a checkout via ``python run.py``.
"""

import sys

from engine.bootstrap import run


if __name__ == "__main__":
    sys.exit(run())
