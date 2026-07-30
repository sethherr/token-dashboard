"""Associate workspace directories with GitHub pull requests.

Off by default. When the ``workspace_pr_links`` setting is on, each workspace
directory is resolved to a label like ``bike_index: PR #4021 - Fix the thing``
so tables show what the work *was* instead of a directory codename — the
motivating case is git-worktree tooling (Conductor et al.) that names
worktrees ``dubai-v3``, ``nukualofa``, ``amsterdam``.

git supplies repo/branch/worktree facts offline; the ``gh`` CLI supplies the
PR. Results are cached in the ``workspace_prs`` table, so the request path
that renders a page is a plain SELECT and never shells out.

Labels, in precedence order:

    main worktree              ``{repo}: main worktree``
    branch with a PR           ``{repo}: PR #{number} - {title}``
    linked worktree, no PR     ``{repo}: {branch}``
    not a git repo             no label (caller keeps the existing name)
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Callable, List, Optional

from .binaries import find_executable

GIT_ENV_VAR = "TOKEN_DASHBOARD_GIT_BIN"
GH_ENV_VAR = "TOKEN_DASHBOARD_GH_BIN"

# Distinguishes "caller didn't say" (auto-detect) from "caller says it's not
# installed" (None). Without it, passing the result of a failed find_git()
# would silently re-run discovery for every workspace.
_AUTO = object()

GIT_TIMEOUT = 5      # local disk reads
GH_TIMEOUT = 20      # network round-trip to github.com

# github.com:owner/name(.git) | github.com/owner/name(.git), ssh or https.
_REMOTE_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


def find_git(env=None, home=None) -> Optional[str]:
    return find_executable("git", env=env, home=home, override_var=GIT_ENV_VAR)


def find_gh(env=None, home=None) -> Optional[str]:
    return find_executable("gh", env=env, home=home, override_var=GH_ENV_VAR)


def _run(args: List[str], timeout: int) -> Optional[str]:
    """Stdout of a successful command, stripped. None on any failure."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def repo_slug(remote_url: Optional[str]) -> Optional[str]:
    """``owner/name`` from a git remote URL, or None if it isn't parseable."""
    if not remote_url:
        return None
    m = _REMOTE_RE.search(remote_url.strip())
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def repo_display(slug: Optional[str]) -> Optional[str]:
    """Bare repo name for display — ``bike_index``, not ``bikeindex/bike_index``."""
    if not slug:
        return None
    return slug.split("/")[-1]


def inspect_workspace(path: str, git_bin=_AUTO, runner: Callable = _run) -> Optional[dict]:
    """git facts for ``path``: repo slug, branch, main-vs-linked worktree.

    None when the path is gone or isn't a git repo — the common case for
    historical transcripts, since worktrees get deleted once merged.
    """
    if git_bin is _AUTO:
        git_bin = find_git()
    if not git_bin or not path:
        return None

    def git(*args):
        return runner([git_bin, "-C", path, *args], GIT_TIMEOUT)

    git_dir = git("rev-parse", "--absolute-git-dir")
    if not git_dir:
        return None
    common_dir = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    # A linked worktree's git dir is <common>/worktrees/<name>; the main
    # worktree's git dir *is* the common dir.
    is_main = bool(common_dir) and git_dir.rstrip("/") == common_dir.rstrip("/")

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        branch = None  # detached; no branch to match a PR against
    slug = repo_slug(git("remote", "get-url", "origin"))
    return {
        "path": path,
        "repo_slug": slug,
        "repo": repo_display(slug),
        "branch": branch,
        "is_main": is_main,
    }


def find_pr(repo_slug_: Optional[str], branch: Optional[str],
            gh_bin=_AUTO, runner: Callable = _run) -> Optional[dict]:
    """The PR whose head is ``branch``, or None.

    Both arguments are required: ``gh pr list --head ""`` silently drops the
    filter and returns whatever PR is newest, which would attach a completely
    unrelated title to the workspace.
    """
    if not repo_slug_ or not branch:
        return None
    if gh_bin is _AUTO:
        gh_bin = find_gh()
    if not gh_bin:
        return None
    out = runner([
        gh_bin, "pr", "list",
        "--repo", repo_slug_,
        "--head", branch,
        "--state", "all",
        "--json", "number,title,state,url",
        "--limit", "1",
    ], GH_TIMEOUT)
    if not out:
        return None
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict) or not row.get("number"):
        return None
    return {
        "number": int(row["number"]),
        "title": str(row.get("title") or "").strip(),
        "state": str(row.get("state") or "").strip(),
        "url": str(row.get("url") or "").strip(),
    }


def build_label(info: Optional[dict], pr: Optional[dict]) -> Optional[str]:
    """Display label for a workspace, or None to keep the existing name."""
    if not info:
        return None
    repo = info.get("repo")
    if not repo:
        return None
    if info.get("is_main"):
        return f"{repo}: main worktree"
    if pr and pr.get("number"):
        title = pr.get("title") or ""
        return f"{repo}: PR #{pr['number']} - {title}" if title else f"{repo}: PR #{pr['number']}"
    branch = info.get("branch")
    if branch:
        return f"{repo}: {branch}"
    return repo


def needs_pr_lookup(info: Optional[dict]) -> bool:
    """Whether resolving this workspace requires a network round-trip."""
    return bool(info) and not info.get("is_main") and bool(info.get("branch")) \
        and bool(info.get("repo_slug"))


def resolve_workspace(path: str, git_bin=_AUTO, gh_bin=_AUTO,
                      runner: Callable = _run, allow_network: bool = True) -> dict:
    """Full resolution for one workspace path. Always returns a row dict.

    ``allow_network=False`` still yields repo/branch/main-worktree labels from
    the local checkout; it just won't ask GitHub for the PR.
    """
    info = inspect_workspace(path, git_bin=git_bin, runner=runner)
    pr = None
    if allow_network and needs_pr_lookup(info):
        # The main worktree's label doesn't depend on a PR, and it's the
        # busiest path in the DB — never spend a round-trip on it.
        pr = find_pr(info.get("repo_slug"), info.get("branch"), gh_bin=gh_bin, runner=runner)
    return {
        "path": path,
        "repo": (info or {}).get("repo"),
        "repo_slug": (info or {}).get("repo_slug"),
        "branch": (info or {}).get("branch"),
        "is_main": 1 if (info or {}).get("is_main") else 0,
        "pr_number": (pr or {}).get("number"),
        "pr_title": (pr or {}).get("title"),
        "pr_url": (pr or {}).get("url"),
        "pr_state": (pr or {}).get("state"),
        "label": build_label(info, pr),
        "resolved": 1 if info else 0,
        "checked_at": time.time(),
    }
