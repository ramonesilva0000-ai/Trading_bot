"""Logging helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional


def setup_logging(level: str = "INFO", file: Optional[str] = None) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if file:
        p = Path(file)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(p, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    return root
