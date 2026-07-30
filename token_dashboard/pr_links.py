"""Associate workspace directories with GitHub pull requests.

Off by default. When the ``workspace_pr_links`` setting is on, each workspace
directory is resolved to a label like ``bike_index: #4021 - Fix the thing``
so tables show what the work *was* instead of a directory codename — the
motivating case is git-worktree tooling (Conductor et al.) that names
worktrees ``dubai-v3``, ``nukualofa``, ``amsterdam``.

git supplies repo/branch/worktree facts offline; the ``gh`` CLI supplies the
PR. Results are cached in the ``workspace_prs`` table, so the request path
that renders a page is a plain SELECT and never shells out.

Labels, in precedence order:

    main worktree              ``{repo}: main worktree``
    branch with a PR           ``{repo}: #{number} - {title}``
    linked worktree, no PR     ``{repo}: {branch}``
    not a git repo             no label (caller keeps the existing name)

Most workspaces in a long history are *deleted* worktrees — the branch merged
and the directory went away — so running git in them is impossible. They are
still resolvable, because two facts survive:

  * the branch, stamped on every transcript message as ``gitBranch``;
  * the repo, inherited from sibling workspaces in the same parent directory
    that *do* still exist (worktree tooling keeps one directory per repo).

Given repo + branch, ``gh pr list --state all`` finds the merged PR. See
``resolve_all``.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Callable, List, Optional, Tuple

from .binaries import find_executable

GIT_ENV_VAR = "TOKEN_DASHBOARD_GIT_BIN"
GH_ENV_VAR = "TOKEN_DASHBOARD_GH_BIN"

# Distinguishes "caller didn't say" (auto-detect) from "caller says it's not
# installed" (None). Without it, passing the result of a failed find_git()
# would silently re-run discovery for every workspace.
_AUTO = object()

GIT_TIMEOUT = 5      # local disk reads
GH_TIMEOUT = 20      # network round-trip to github.com
GH_BULK_TIMEOUT = 120  # one call can page through hundreds of PRs

# One bulk fetch per repo beats one query per branch: ~5s for 1000 PRs versus
# ~0.5s x N. Branches not covered by the bulk window fall back to a targeted
# query, bounded by the caller's lookup budget.
DEFAULT_PR_FETCH = 1000

# Branch names that are never a PR head worth attributing, and which collide
# across repos — attributing one of these by inference would be a guess.
GENERIC_BRANCHES = frozenset({"main", "master", "develop", "trunk", "HEAD"})

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
        # Bare "#123", not "PR #123" — the frontend turns the number into a
        # link to the PR, so the word would just be noise.
        return f"{repo}: #{pr['number']} - {title}" if title else f"{repo}: #{pr['number']}"
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


def fetch_repo_prs(repo_slug_: str, gh_bin=_AUTO, runner: Callable = _run,
                   limit: int = DEFAULT_PR_FETCH) -> dict:
    """``{head_branch: pr}`` for a repo, in one call.

    Where a branch name was reused across PRs, the highest-numbered (most
    recent) one wins — that's the PR the workspace most likely belonged to.
    """
    if not repo_slug_:
        return {}
    if gh_bin is _AUTO:
        gh_bin = find_gh()
    if not gh_bin:
        return {}
    out = runner([
        gh_bin, "pr", "list",
        "--repo", repo_slug_,
        "--state", "all",
        "--json", "number,title,state,url,headRefName",
        "--limit", str(limit),
    ], GH_BULK_TIMEOUT)
    if not out:
        return {}
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return {}
    if not isinstance(rows, list):
        return {}
    index: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        head = row.get("headRefName")
        number = row.get("number")
        if not head or not number:
            continue
        prev = index.get(head)
        if prev and prev["number"] >= number:
            continue
        index[head] = {
            "number": int(number),
            "title": str(row.get("title") or "").strip(),
            "state": str(row.get("state") or "").strip(),
            "url": str(row.get("url") or "").strip(),
        }
    return index


def search_pr(branch: Optional[str], owners=(), gh_bin=_AUTO,
              runner: Callable = _run, prefer_repo: Optional[str] = None) -> Optional[dict]:
    """Find a PR by head branch across repos, via GitHub's search API.

    The last resort for a workspace whose directory is gone *and* whose repo
    can't be inferred from siblings. Unlike the other paths this doesn't need
    to know the repo — the search result reports it, which also means the
    answer is corroborated by GitHub rather than guessed.

    Ambiguity is refused: if the branch name matches PRs in more than one
    repo, we return nothing instead of picking one — unless ``prefer_repo``
    names one of them. That hint is the workspace's parent directory, which
    worktree tooling names after the repo, so it discriminates between two
    repos that genuinely share a branch name (common when syncing shared
    tooling between projects on identically-named branches). It only ever
    chooses among candidates GitHub itself returned.
    """
    if not branch or branch in GENERIC_BRANCHES:
        return None
    if gh_bin is _AUTO:
        gh_bin = find_gh()
    if not gh_bin:
        return None
    args = [gh_bin, "search", "prs", "--head", branch,
            "--json", "number,title,repository,state,url", "--limit", "5"]
    for owner in owners:
        args += ["--owner", owner]
    out = runner(args, GH_TIMEOUT)
    if not out:
        return None
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    rows = [r for r in rows if isinstance(r, dict) and r.get("number")]
    if not rows:
        return None
    repos = {(r.get("repository") or {}).get("nameWithOwner") for r in rows}
    repos.discard(None)
    if len(repos) != 1:
        # Same branch name in several repos. Only the directory hint can break
        # the tie; without a match, refuse rather than guess.
        hint = (prefer_repo or "").strip().lower()
        matches = [r for r in rows
                   if hint and (repo_display((r.get("repository") or {}).get("nameWithOwner")) or "").lower() == hint]
        if not matches:
            return None
        rows = matches
        repos = {(matches[0].get("repository") or {}).get("nameWithOwner")}
    best = max(rows, key=lambda r: r["number"])
    return {
        "number": int(best["number"]),
        "title": str(best.get("title") or "").strip(),
        # search returns lowercase states ("merged"); pr list returns "MERGED".
        "state": str(best.get("state") or "").strip().upper(),
        "url": str(best.get("url") or "").strip(),
        "repo_slug": repos.pop(),
    }


def parent_dir_name(path: str) -> Optional[str]:
    """Basename of a workspace's parent directory — the repo, by convention."""
    parent = _parent_dir(path)
    if not parent:
        return None
    sep = "\\" if "\\" in parent else "/"
    name = parent.rstrip(sep).rsplit(sep, 1)[-1]
    return name or None


def _parent_dir(path: str) -> str:
    trimmed = (path or "").rstrip("/\\")
    sep = "\\" if "\\" in trimmed else "/"
    return trimmed.rsplit(sep, 1)[0] if sep in trimmed else ""


def infer_repos_by_sibling(live_repos: dict) -> dict:
    """``{parent_dir: repo_slug}`` for parents whose live children all agree.

    Worktree tooling keeps one directory per repo
    (``~/conductor/workspaces/<repo>/<workspace>``), so a deleted workspace's
    repo is whatever its surviving siblings are. Unanimity is required: a
    parent like ``~/Sites`` holding several unrelated projects yields nothing
    rather than a guess.
    """
    by_parent: dict = {}
    for path, slug in live_repos.items():
        if not slug:
            continue
        by_parent.setdefault(_parent_dir(path), set()).add(slug)
    return {parent: next(iter(slugs)) for parent, slugs in by_parent.items() if len(slugs) == 1}


def _row(path, repo_slug_=None, branch=None, is_main=False, pr=None,
         resolved=0, inferred=0) -> dict:
    repo = repo_display(repo_slug_)
    info = {"repo": repo, "is_main": is_main, "branch": branch}
    return {
        "path": path,
        "repo": repo,
        "repo_slug": repo_slug_,
        "branch": branch,
        "is_main": 1 if is_main else 0,
        "pr_number": (pr or {}).get("number"),
        "pr_title": (pr or {}).get("title"),
        "pr_url": (pr or {}).get("url"),
        "pr_state": (pr or {}).get("state"),
        "label": build_label(info, pr),
        "resolved": resolved,
        "inferred": inferred,
        "checked_at": time.time(),
    }


def resolve_all(paths, recorded_branches=None, git_bin=_AUTO, gh_bin=_AUTO,
                runner: Callable = _run, bulk_limit: int = DEFAULT_PR_FETCH,
                max_lookups: int = 200, max_searches: int = 100,
                on_progress: Optional[Callable] = None,
                repo_hints=None) -> Tuple[List[dict], dict]:
    """Resolve every workspace path, including ones no longer on disk.

    Four phases, cheapest first:

    1. **git**, for paths that still exist — repo, branch, main-vs-linked.
    2. **inference**, for paths that don't — branch from the transcripts,
       repo from unanimous live siblings under the same parent directory.
    3. **one bulk PR fetch per repo**, matched against branches offline.
    4. **targeted PR queries** for branches the bulk window missed, bounded
       by ``max_lookups``.
    5. **cross-repo search** for workspaces with a branch but no repo at all
       (no live siblings to inherit from), bounded by ``max_searches``. The
       search reports the repo, so nothing here is guessed; where a branch
       name exists in two repos, the workspace's parent directory name breaks
       the tie.

    ``bulk_limit=0`` skips phase 3 entirely — right for incremental top-ups of
    a handful of workspaces, where fetching a repo's whole PR list would cost
    far more than querying each branch.

    ``repo_hints`` seeds phase 2 with ``{parent_dir: repo_slug}`` learned
    elsewhere. It matters when resolving a *subset* of workspaces: sibling
    inference can only see the paths it is given, so an incremental pass over
    a few dead worktrees would otherwise find no live sibling and conclude the
    repo is unknown — discarding an attribution an earlier full pass made.

    ``on_progress``, if given, is called with
    ``{"phase", "done", "total", "detail"}`` as work proceeds — a full refresh
    takes the better part of a minute, so the UI needs something to show.

    Returns ``(rows, stats)``.
    """
    recorded = dict(recorded_branches or {})
    if git_bin is _AUTO:
        git_bin = find_git()
    if gh_bin is _AUTO:
        gh_bin = find_gh()
    paths = list(paths)

    def progress(phase, done=0, total=0, detail=None):
        if on_progress:
            try:
                on_progress({"phase": phase, "done": done, "total": total, "detail": detail})
            except Exception:
                pass  # a reporting failure must never abort the refresh

    # ── 1. local git ─────────────────────────────────────────────────────────
    progress("inspect", 0, len(paths))
    live: dict = {}
    for i, path in enumerate(paths, 1):
        info = inspect_workspace(path, git_bin=git_bin, runner=runner)
        if info:
            live[path] = info
        progress("inspect", i, len(paths), path)

    # ── 2. inference for the rest ────────────────────────────────────────────
    # Locally observed siblings win over hints: they reflect the disk right now.
    sibling_repo = dict(repo_hints or {})
    sibling_repo.update(infer_repos_by_sibling({p: i.get("repo_slug") for p, i in live.items()}))
    plan: dict = {}
    for path in paths:
        info = live.get(path)
        if info:
            plan[path] = {
                "repo_slug": info.get("repo_slug"),
                "branch": info.get("branch") or recorded.get(path),
                "is_main": info.get("is_main"),
                "resolved": 1,
                "inferred": 0,
            }
            continue
        branch = recorded.get(path)
        slug = sibling_repo.get(_parent_dir(path))
        if not branch or not slug or branch in GENERIC_BRANCHES:
            plan[path] = {"repo_slug": None, "branch": branch, "is_main": False,
                          "resolved": 0, "inferred": 0}
            continue
        plan[path] = {"repo_slug": slug, "branch": branch, "is_main": False,
                      "resolved": 1, "inferred": 1}

    # ── 3. one bulk PR index per repo ────────────────────────────────────────
    wanted_repos = {
        p["repo_slug"] for p in plan.values()
        if p["repo_slug"] and p["branch"] and not p["is_main"]
    }
    indexes: dict = {}
    if bulk_limit > 0:
        total_repos = len(wanted_repos)
        progress("repos", 0, total_repos)
        for i, slug in enumerate(sorted(wanted_repos), 1):
            progress("repos", i - 1, total_repos, slug)
            indexes[slug] = fetch_repo_prs(slug, gh_bin=gh_bin, runner=runner, limit=bulk_limit)
            progress("repos", i, total_repos, slug)

    # ── 4. targeted lookups for what the bulk window missed ──────────────────
    owners = sorted({slug.split("/")[0] for slug in wanted_repos if "/" in slug})
    rows, lookups, throttled, searches, searched_ok = [], 0, 0, 0, 0
    # Same branch in the same unknown repo appears under several workspaces;
    # search once and reuse.
    search_cache: dict = {}
    progress("match", 0, len(paths))
    for done, path in enumerate(paths, 1):
        p = plan[path]
        pr = None
        repo_slug_ = p["repo_slug"]
        resolved, inferred = p["resolved"], p["inferred"]
        if repo_slug_ and p["branch"] and not p["is_main"]:
            pr = indexes.get(repo_slug_, {}).get(p["branch"])
            if pr is None:
                if lookups < max_lookups:
                    lookups += 1
                    pr = find_pr(repo_slug_, p["branch"], gh_bin=gh_bin, runner=runner)
                else:
                    throttled += 1
        elif not repo_slug_ and p["branch"] and p["branch"] not in GENERIC_BRANCHES:
            # No repo to query — ask GitHub which repo this branch belongs to.
            branch = p["branch"]
            # Cache per (branch, hint): the same branch under two different
            # parent directories can legitimately resolve to different repos.
            hint = parent_dir_name(path)
            key = (branch, hint)
            if key in search_cache:
                pr = search_cache[key]
            elif searches < max_searches:
                searches += 1
                pr = search_pr(branch, owners=owners, gh_bin=gh_bin, runner=runner,
                               prefer_repo=hint)
                search_cache[key] = pr
            else:
                throttled += 1
            if pr:
                repo_slug_ = pr.get("repo_slug") or repo_slug_
                resolved, inferred = 1, 1
                searched_ok += 1
        rows.append(_row(path, repo_slug_, p["branch"], p["is_main"], pr,
                         resolved=resolved, inferred=inferred))
        progress("match", done, len(paths), path)
    stats = {
        "checked": len(paths),
        "live": len(live),
        "inferred": sum(1 for r in rows if r["inferred"]),
        "resolved": sum(1 for r in rows if r["resolved"]),
        "with_pr": sum(1 for r in rows if r["pr_number"]),
        "repos": len(indexes),
        "bulk_prs": sum(len(i) for i in indexes.values()),
        "pr_lookups": lookups,
        "searches": searches,
        "found_by_search": searched_ok,
        "throttled": throttled,
    }
    return rows, stats
