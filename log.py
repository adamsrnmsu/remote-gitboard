"""Rich console + logging, as singletons.

Two consoles on purpose:

  out() -> stdout, for the board itself. `gitboard show --markdown | less`
           and the /board command both read this, so nothing else may write
           to it or the output stops being parseable.
  err() -> stderr, for logs, progress, and status. Safe to be chatty.

Rich detects a pipe and drops colour automatically, so piped output stays
plain without a --no-color flag.
"""

import logging
from functools import lru_cache

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

THEME = Theme(
    {
        "logging.level.debug": "dim cyan",
        "logging.level.info": "green",
        "logging.level.warning": "yellow",
        "logging.level.error": "bold red",
        "added": "green",
        "changed": "yellow",
        "col": "bold cyan",
        "muted": "dim",
    }
)


@lru_cache(maxsize=1)
def out() -> Console:
    """stdout — the payload. Keep it clean and pipeable."""
    return Console(theme=THEME, soft_wrap=True)


@lru_cache(maxsize=1)
def err() -> Console:
    """stderr — logs, progress, anything humans read but pipes shouldn't."""
    return Console(theme=THEME, stderr=True)


@lru_cache(maxsize=1)
def _handler() -> RichHandler:
    return RichHandler(
        console=err(),
        rich_tracebacks=True,
        show_path=False,
        show_time=False,
        markup=True,
    )


@lru_cache(maxsize=1)
def get_logger() -> logging.Logger:
    logger = logging.getLogger("gitboard")
    logger.addHandler(_handler())
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't double-print via the root logger
    return logger


def set_verbose(verbose: bool) -> None:
    get_logger().setLevel(logging.DEBUG if verbose else logging.INFO)


def reset() -> None:
    """Drop the cached consoles and logger. For tests."""
    logger = get_logger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    for fn in (out, err, _handler, get_logger):
        fn.cache_clear()
