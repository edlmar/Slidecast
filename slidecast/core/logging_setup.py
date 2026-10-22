"""Stdlib logging configured for file + console + (optionally) a GUI pane."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ParamSpec, TypeVar

LOGGER_NAME = "slidecast"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


@contextmanager
def timed_stage(name: str, **fields: object) -> Iterator[None]:
    """Emit a machine-parseable ``stage=NAME seconds=N.NN ok=0|1 k=v ...`` record on exit.

    Stage timings land in the rotating log as fields (``key=value``), not prose, so they
    can be grepped or parsed back out of a run log - including for stages that failed.
    """
    t0 = time.perf_counter()
    ok = 1
    try:
        yield
    except BaseException:
        ok = 0
        raise
    finally:
        elapsed = time.perf_counter() - t0
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        get_logger().info(
            "stage=%s seconds=%.2f ok=%d%s", name, elapsed, ok, f" {extra}" if extra else ""
        )


_P = ParamSpec("_P")
_R = TypeVar("_R")


def timed(name: str, **fields: object) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorator form of :func:`timed_stage` for whole-function stage timing."""
    import functools

    def deco(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(fn)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with timed_stage(name, **fields):
                return fn(*args, **kwargs)

        return wrapper

    return deco


class CallbackHandler(logging.Handler):
    """Forwards formatted records to a callable (used by the GUI log console)."""

    def __init__(self, callback: Callable[[str, int], None]) -> None:
        super().__init__()
        self._callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(self.format(record), record.levelno)
        except Exception:
            self.handleError(record)


def configure_logging(
    log_file: Path | None = None,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """Configure the Slidecast logger. Safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        if getattr(h, "_slidecast_managed", False):
            logger.removeHandler(h)
            h.close()

    # A windowed build (no console) has sys.stderr set to None; a StreamHandler on it would
    # swallow every record through handleError instead of logging it.
    if console and sys.stderr is not None:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(level)
        ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        ch._slidecast_managed = True  # type: ignore[attr-defined]
        logger.addHandler(ch)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(_FORMAT))
        fh._slidecast_managed = True  # type: ignore[attr-defined]
        logger.addHandler(fh)
    return logger
