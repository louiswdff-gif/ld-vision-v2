#!/usr/bin/env python3
"""Point d'entrée : `python main.py` (voir `python main.py --help`)."""

import sys

from bot_vinted.cli import principal

if __name__ == "__main__":
    sys.exit(principal())
