"""Logging configuration: concise progress on stdout, verbose detail to file."""

from __future__ import annotations

import logging
import sys


def setup_logging(log_file: str | None, verbose: bool = False) -> logging.Logger:
    root = logging.getLogger("rt2nb")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(stream)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s %(message)s"
            )
        )
        root.addHandler(fh)

    root.propagate = False
    return root


def get_logger(name: str = "rt2nb") -> logging.Logger:
    return logging.getLogger(name)
