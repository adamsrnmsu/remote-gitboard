"""Shared test setup.

Colour off before anything imports rich: CLI tests assert on rendered text,
and a FORCE_COLOR in the environment (some agent shells export it) splits that
text with ANSI codes. log.py builds its consoles at import, so a fixture would
be too late.
"""

import os

os.environ.pop("FORCE_COLOR", None)
