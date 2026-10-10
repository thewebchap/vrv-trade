#!/usr/bin/env python3
"""Entry point: python tradepilot.py Documents/Metals/1   |   python tradepilot.py Documents/Metals"""
import sys

from tradepilot.cli import main

if __name__ == "__main__":
    sys.exit(main())
