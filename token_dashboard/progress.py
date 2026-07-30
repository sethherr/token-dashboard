"""Terminal progress rendering for long scans.

The first scan of a heavy user's ``~/.claude/projects/`` can take a minute,
so ``scan_dir`` accepts a progress callback and this module renders it as a
single rewritten line. On a non-tty (pipes, CI, ``| tee``) the renderer is a
no-op so logs stay clean.
"""
from __future__ import annotations

import sys
import time
from typing import Optional, TextIO


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


class TerminalProgress:
    """Callable progress sink: ``progress(done_files, total_files, done_bytes, total_bytes)``."""

    def __init__(
        self,
        stream: Optional[TextIO] = None,
        label: str = "Scanning transcripts",
        width: int = 24,
        min_interval: float = 0.1,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.label = label
        self.width = width
        self.min_interval = min_interval
        self.enabled = bool(getattr(self.stream, "isatty", lambda: False)())
        self._last_emit = 0.0
        self._last_pct = -1
        self._line_len = 0
        encoding = (getattr(self.stream, "encoding", None) or "").lower()
        self._blocks = ("█", "░") if "utf" in encoding else ("#", "-")

    def __call__(self, done_files: int, total_files: int, done_bytes: int, total_bytes: int) -> None:
        if not self.enabled:
            return
        pct = int(done_bytes * 100 / total_bytes) if total_bytes else 100
        now = time.monotonic()
        final = total_files and done_files >= total_files
        if not final and pct == self._last_pct and (now - self._last_emit) < self.min_interval:
            return
        self._last_emit = now
        self._last_pct = pct
        filled = round(self.width * pct / 100)
        bar = self._blocks[0] * filled + self._blocks[1] * (self.width - filled)
        line = (
            f"{self.label} [{bar}] {pct:3d}%  "
            f"{done_files}/{total_files} files  {_human_bytes(done_bytes)}"
        )
        self._write("\r" + line.ljust(self._line_len))
        self._line_len = len(line)

    def finish(self) -> None:
        """Erase the progress line so the caller's summary starts clean."""
        if not self.enabled or not self._line_len:
            return
        self._write("\r" + " " * self._line_len + "\r")
        self._line_len = 0

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (UnicodeEncodeError, ValueError, OSError):
            self.enabled = False
