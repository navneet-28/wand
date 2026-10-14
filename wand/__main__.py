"""Allows `python -m wand ...`."""

import sys

from wand.cli import main

sys.exit(main())
