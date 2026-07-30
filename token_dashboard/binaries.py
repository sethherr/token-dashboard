"""Locating external CLI tools (git, gh, rtk).

None of these install to a single canonical place — Homebrew, Cargo, and the
various install scripts all differ — and a dashboard launched from a GUI or
launchd context inherits a minimal PATH that often omits Homebrew. So: check
an explicit override, then PATH, then the usual suspects.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

COMMON_BIN_DIRS = (
    ("~", ".local", "bin"),
    ("~", ".cargo", "bin"),
    ("/opt", "homebrew", "bin"),
    ("/usr", "local", "bin"),
    ("/usr", "bin"),
    ("/bin",),
)


def _candidates(name: str, home_path: Path) -> list:
    out = []
    for parts in COMMON_BIN_DIRS:
        base = home_path.joinpath(*parts[1:]) if parts[0] == "~" else Path(*parts)
        out.append(base / name)
        out.append(base / f"{name}.exe")  # Windows
    return out


def find_executable(
    name: str,
    env=None,
    home=None,
    override_var: Optional[str] = None,
) -> Optional[str]:
    """Absolute path to ``name``, or None if it isn't installed.

    ``override_var`` names an environment variable holding an explicit path.
    An override that doesn't resolve returns None rather than falling back —
    if you point us at a binary and we can't run it, that's worth surfacing,
    not papering over with a different one.
    """
    env = os.environ if env is None else env
    home_path = Path(home) if home is not None else Path.home()

    if override_var:
        override = env.get(override_var)
        if override:
            ok = Path(override).is_file() and os.access(override, os.X_OK)
            return override if ok else None

    # PATH default is "" rather than None so a caller-supplied env without
    # PATH searches nothing instead of falling back to os.defpath.
    found = shutil.which(name, path=env.get("PATH", ""))
    if found:
        return found

    for cand in _candidates(name, home_path):
        if cand.is_file() and os.access(str(cand), os.X_OK):
            return str(cand)
    return None
