"""HTTP server: static frontend + JSON endpoints + SSE diff stream."""
from __future__ import annotations

import datetime
import http.server
import json
import mimetypes
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

from . import pr_links
from .db import (
    clear_scan_data, default_claude_dir, get_setting, set_setting,
    workspace_pr_map, save_workspace_prs, workspace_root_paths, clear_workspace_prs,
    workspace_branches, workspace_paths_needing_pr_refresh,
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions, session_turns,
    session_model_tokens,
    daily_token_breakdown, model_breakdown, skill_breakdown,
    workspaces_matrix, cross_workspace_leaks,
    subagent_breakdown, top_subagent_sessions,
    orchestration_breakdown, dispatch_tree,
)
from .pricing import load_pricing, cost_for, get_plan, set_plan
from .tips import all_tips, dismiss_tip
from .scanner import scan_dir
from .skills import cached_catalog
from .plugins import cached_plugins
from .mcp_catalog import scan_mcp
from .hooks_catalog import scan_hooks, scan_commands, scan_agents


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"

# Server-sent events fan out to every open /api/stream connection. A single
# shared queue.Queue would hand each event to just one connection (whichever
# get() wins), so with two browser tabs open only one would see a scan/error
# event. Each connection subscribes its own queue; producers broadcast to all.
_EVENT_SUBS: "set[queue.Queue[dict]]" = set()
_EVENT_SUBS_LOCK = threading.Lock()


def _subscribe() -> "queue.Queue[dict]":
    q: "queue.Queue[dict]" = queue.Queue()
    with _EVENT_SUBS_LOCK:
        _EVENT_SUBS.add(q)
    return q


def _unsubscribe(q) -> None:
    with _EVENT_SUBS_LOCK:
        _EVENT_SUBS.discard(q)


def _publish_event(evt: dict) -> None:
    with _EVENT_SUBS_LOCK:
        subs = list(_EVENT_SUBS)
    for q in subs:
        q.put(evt)


# Keep cache resets and concurrent scans from interleaving with background scans.
SCAN_LOCK = threading.Lock()

MAX_POST_BYTES = 1_000_000  # 1 MB — we only accept tiny JSON bodies (settings, plan, tip key)
MAX_LIMIT = 1000

# Simple in-process response cache. TTL is slightly under the scan interval so
# cached data is never more than one scan cycle stale. Cleared after each scan.
_CACHE: "dict[str, dict]" = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 300.0  # 5 min safety net; scan loop clears explicitly on new data


def _cache_get(key: str):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and time.time() - entry["ts"] < _CACHE_TTL:
            return entry["data"]
    return None


def _cache_set(key: str, data) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data}


def _cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _bundle_cache_key(since, until) -> str:
    # Truncate to date so the key is stable within a day, enabling pre-warming.
    s = since[:10] if since else ""
    u = until[:10] if until else ""
    return f"/api/overview-bundle?since={s}&until={u}"


def _overview_bundle(db_path: str, since, until, pricing: dict) -> dict:
    """Run all overview-page queries and return a single combined payload."""
    by_model = model_breakdown(db_path, since, until)
    totals = overview_totals(db_path, since, until)
    cost_usd = 0.0
    for m in by_model:
        c = cost_for(m["model"], m, pricing)
        m["cost_usd"] = c["usd"]
        m["cost_estimated"] = c["estimated"]
        if c["usd"] is not None:
            cost_usd += c["usd"]
    totals["cost_usd"] = round(cost_usd, 4)
    return {
        "totals": totals,
        "projects": _apply_workspace_labels(db_path, project_summary(db_path, since, until)),
        "sessions": _apply_workspace_labels(
            db_path, recent_sessions(db_path, limit=10, since=since, until=until)),
        "tools": tool_token_breakdown(db_path, since, until),
        "daily": daily_token_breakdown(db_path, since, until),
        "byModel": by_model,
    }


_WARM_DAYS = [7, 30, 90, None]  # None = all time
_WARM_DEFAULT_DAYS = 30          # the range the UI lands on first


def _do_refresh(db_path: str, projects_dir: str, pricing: dict) -> None:
    """One-shot scan + cache-clear + warm, used by the manual /api/refresh endpoint."""
    if not SCAN_LOCK.acquire(blocking=False):
        _publish_event({"type": "scan-skip", "reason": "already-running", "ts": time.time()})
        return
    try:
        n = scan_dir(_projects_dir(db_path, projects_dir), db_path)
        _top_up_workspace_prs(db_path)
        _cache_clear()
        _publish_event({"type": "scan", "n": n, "ts": time.time()})
    except Exception as e:
        _publish_event({"type": "error", "message": str(e)})
    finally:
        SCAN_LOCK.release()


def _warm_one(db_path: str, pricing: dict, days) -> None:
    try:
        since = (
            (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
            if days else None
        )
        key = _bundle_cache_key(since, None)
        if _cache_get(key) is None:
            _cache_set(key, _overview_bundle(db_path, since, None, pricing))
    except Exception:
        pass


def _warm_bundle(db_path: str, pricing: dict) -> None:
    """Pre-warm all time-range bundles serially.

    Serial — was 4 parallel threads each on its own SQLite connection, which
    caused severe contention with scan writes on cold boot.
    """
    for days in _WARM_DAYS:
        _warm_one(db_path, pricing, days)


def _send_json(handler, obj, status: int = 200) -> None:
    body = json.dumps(obj, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_error(handler, status: int, msg: str) -> None:
    _send_json(handler, {"error": msg}, status=status)


def _clamp_limit(raw, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, MAX_LIMIT))


def _empty_rtk_payload(available: bool) -> dict:
    return {
        "available": available,
        "install_url": "https://github.com/rtk-ai/rtk",
        "summary": None,
        "daily": [],
        "weekly": [],
        "monthly": [],
    }


RTK_ENV_VAR = "TOKEN_DASHBOARD_RTK_BIN"

# Searched in order after PATH, since a dashboard launched from a GUI/launchd
# context often inherits a minimal PATH that omits Homebrew and Cargo.
_RTK_FALLBACK_DIRS = (
    ("~", ".local", "bin"),
    ("~", ".cargo", "bin"),
    ("/opt", "homebrew", "bin"),
    ("/usr", "local", "bin"),
)


def _rtk_fallback_paths(home_path: Path) -> list:
    out = []
    for parts in _RTK_FALLBACK_DIRS:
        base = home_path.joinpath(*parts[1:]) if parts[0] == "~" else Path(*parts)
        out.append(base / "rtk")
        out.append(base / "rtk.exe")  # Windows
    return out


def _find_rtk(home=None, env=None) -> Optional[str]:
    """Locate the ``rtk`` binary, or None if it isn't installed.

    Resolution order: the ``TOKEN_DASHBOARD_RTK_BIN`` override, then ``PATH``,
    then a handful of common install dirs. RTK is not installed to a single
    canonical location — Homebrew, Cargo, and the install script each put it
    somewhere different — so probing one hardcoded path misses most setups.
    """
    env = os.environ if env is None else env
    home_path = Path(home) if home is not None else Path.home()

    override = env.get(RTK_ENV_VAR)
    if override:
        # An explicit override is honoured as given: if it's wrong, the RTK tab
        # should report "not installed" rather than silently use a different binary.
        return override if os.access(override, os.X_OK) and Path(override).is_file() else None

    # Pass PATH explicitly (defaulting to empty, not None) so a caller-supplied
    # env without PATH searches nothing rather than falling back to os.defpath.
    found = shutil.which("rtk", path=env.get("PATH", ""))
    if found:
        return found

    for cand in _rtk_fallback_paths(home_path):
        if cand.is_file() and os.access(str(cand), os.X_OK):
            return str(cand)
    return None


def _rtk_payload(home=None, env=None) -> dict:
    rtk_bin = _find_rtk(home=home, env=env)
    if not rtk_bin:
        return _empty_rtk_payload(False)
    run_env = dict(os.environ if env is None else env)
    # Make sure the resolved binary's own directory is reachable, in case rtk
    # shells out to siblings.
    parent = str(Path(rtk_bin).parent)
    if parent not in run_env.get("PATH", "").split(os.pathsep):
        run_env["PATH"] = parent + os.pathsep + run_env.get("PATH", "")
    try:
        r = subprocess.run(
            [rtk_bin, "gain", "--format", "json", "--all"],
            capture_output=True, text=True, timeout=10, env=run_env,
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            # A different tool also ships as `rtk` (Rust Type Kit); it can exit 0
            # with output that isn't the gain report, so don't trust the shape.
            if isinstance(data, dict):
                data["available"] = True
                data["install_url"] = "https://github.com/rtk-ai/rtk"
                return data
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return _empty_rtk_payload(True)



def _resolve_static(root: Path, rel: str) -> Optional[Path]:
    """Resolve a request path under ``root``, or None if it escapes or is not a file.

    Containment uses ``relative_to``: a plain ``startswith(str(root))`` check
    would accept a sibling directory whose name shares the root's prefix
    (e.g. ``root`` + "-secret"), since the separator isn't part of the compare.
    """
    p = (root / rel.lstrip("/")).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


def _serve_static(handler, rel: str) -> None:
    p = _resolve_static(WEB_ROOT, rel)
    if p is None:
        handler.send_response(404)
        handler.end_headers()
        return
    body = p.read_bytes()
    ctype, _ = mimetypes.guess_type(str(p))
    handler.send_response(200)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _mcp_usage_calls(server_name: str, tools) -> Optional[int]:
    """Total tool-call count for one MCP server.

    MCP tool names are ``mcp__<server>__<tool>``; match the server segment
    exactly so a short name ("git") does not absorb another server's calls
    ("github"). ``tools`` is a tool_token_breakdown list. Returns None when the
    server has no recorded calls.
    """
    norm = server_name.lower().replace(" ", "_")
    total = 0
    for t in tools:
        parts = t["tool_name"].lower().split("__")
        if len(parts) >= 3 and parts[0] == "mcp" and parts[1] == norm:
            total += t["calls"]
    return total or None


def _claude_dir(db_path: str) -> Path:
    saved = get_setting(db_path, "claude_dir")
    return Path(saved).expanduser() if saved else default_claude_dir()


def _claude_dirs(db_path: str) -> list[str]:
    active = str(_claude_dir(db_path))
    raw = get_setting(db_path, "claude_dirs")
    dirs = []
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            dirs = [str(p) for p in parsed if isinstance(p, str) and p]
    out = []
    for path in [active, *dirs]:
        if path not in out:
            out.append(path)
    return out


def _remember_claude_dir(db_path: str, claude_dir: Path) -> None:
    path = str(claude_dir)
    dirs = [path, *[p for p in _claude_dirs(db_path) if p != path]]
    set_setting(db_path, "claude_dirs", json.dumps(dirs))


def _projects_dir(db_path: str, projects_override: Optional[str] = None) -> Path:
    if projects_override:
        return Path(projects_override).expanduser()
    return _claude_dir(db_path) / "projects"


def _validate_claude_dir(raw) -> Tuple[Optional[Path], Optional[str]]:
    if not isinstance(raw, str) or not raw.strip():
        return None, "claude_dir is required"
    path = Path(raw.strip()).expanduser()
    if not path.exists():
        return None, f"{path} does not exist"
    if not path.is_dir():
        return None, f"{path} is not a directory"
    projects = path / "projects"
    if projects.exists() and not projects.is_dir():
        return None, f"{projects} exists but is not a directory"
    return path, None


# Budget the expensive thing — targeted GitHub round-trips. Bulk PR fetches
# are one call per repo regardless of how many workspaces map to it.
MAX_WORKSPACE_PR_LOOKUPS = 200
MAX_WORKSPACE_PATHS = 5000  # runaway guard only


def _refresh_workspace_prs(db_path: str, paths=None, resolver=None, on_progress=None) -> dict:
    """Resolve every known workspace to its repo/PR and cache the result.

    Deleted worktrees are resolved too: the branch comes from the transcripts
    (``messages.git_branch``) and the repo from surviving sibling workspaces,
    so a merged-and-deleted branch still finds its PR. See
    ``pr_links.resolve_all``.
    """
    resolver = resolver or pr_links.resolve_all
    paths = list(paths if paths is not None else workspace_root_paths(db_path))
    truncated = 0
    if len(paths) > MAX_WORKSPACE_PATHS:
        truncated = len(paths) - MAX_WORKSPACE_PATHS
        paths = paths[:MAX_WORKSPACE_PATHS]
    git_bin = pr_links.find_git()
    gh_bin = pr_links.find_gh()
    try:
        rows, stats = resolver(
            paths,
            recorded_branches=workspace_branches(db_path),
            git_bin=git_bin, gh_bin=gh_bin,
            max_lookups=MAX_WORKSPACE_PR_LOOKUPS,
            on_progress=on_progress,
        )
    except Exception as e:
        return {"checked": 0, "resolved": 0, "with_pr": 0, "error": str(e),
                "gh_available": bool(gh_bin), "git_available": bool(git_bin)}
    save_workspace_prs(db_path, rows)
    return {**stats, "skipped": truncated,
            "gh_available": bool(gh_bin), "git_available": bool(git_bin)}


# A workspace usually gets its PR after work has already started, so a
# PR-less workspace is re-checked periodically rather than once.
WORKSPACE_PR_RECHECK_SECONDS = 900.0
MAX_INCREMENTAL_PR_LOOKUPS = 25


def _top_up_workspace_prs(db_path: str, resolver=None, now=None) -> dict:
    """Resolve workspaces whose PR link is missing, as part of a normal refresh.

    Runs after every scan when the setting is on, so newly-created worktrees
    (and workspaces that have since had a PR opened) pick up their label
    without anyone pressing "Refresh PR links". Targeted queries only, and
    capped — a scan must never turn into a long network stall.
    """
    if not workspace_pr_links_enabled(db_path):
        return {"checked": 0, "skipped_reason": "disabled"}
    now = time.time() if now is None else now
    paths = workspace_paths_needing_pr_refresh(
        db_path, recheck_before=now - WORKSPACE_PR_RECHECK_SECONDS)
    if not paths:
        return {"checked": 0}
    capped = paths[:MAX_INCREMENTAL_PR_LOOKUPS]
    resolver = resolver or pr_links.resolve_all
    try:
        rows, stats = resolver(
            capped,
            recorded_branches=workspace_branches(db_path),
            git_bin=pr_links.find_git(), gh_bin=pr_links.find_gh(),
            bulk_limit=0,  # a handful of paths: query branches, don't page whole repos
            max_lookups=MAX_INCREMENTAL_PR_LOOKUPS,
        )
    except Exception as e:
        return {"checked": 0, "error": str(e)}
    save_workspace_prs(db_path, rows)
    return {**stats, "pending": max(0, len(paths) - len(capped))}


# One refresh at a time: it's a long network job, and two concurrent passes
# would interleave their progress events into nonsense.
WORKSPACE_PR_LOCK = threading.Lock()
_WORKSPACE_PR_PROGRESS: dict = {"running": False}
_WORKSPACE_PR_PROGRESS_LOCK = threading.Lock()
_PROGRESS_MIN_INTERVAL = 0.2  # seconds between pushed events


def workspace_pr_progress() -> dict:
    with _WORKSPACE_PR_PROGRESS_LOCK:
        return dict(_WORKSPACE_PR_PROGRESS)


def _set_workspace_pr_progress(**fields) -> dict:
    with _WORKSPACE_PR_PROGRESS_LOCK:
        _WORKSPACE_PR_PROGRESS.update(fields)
        return dict(_WORKSPACE_PR_PROGRESS)


def _refresh_workspace_prs_async(db_path: str, resolver=None) -> dict:
    """Kick off a full refresh in the background, streaming progress over SSE.

    Returns immediately: a full pass is ~a minute of network calls, far too
    long to hold a request open. Progress arrives as ``workspace-prs`` events
    and is also readable from ``/api/workspace-prs/status`` for a client that
    reconnects mid-run.
    """
    if not WORKSPACE_PR_LOCK.acquire(blocking=False):
        return {"started": False, "reason": "already-running", **workspace_pr_progress()}

    state = _set_workspace_pr_progress(
        running=True, phase="starting", done=0, total=0, detail=None,
        started_at=time.time(), finished_at=None, stats=None, error=None)
    _publish_event({"type": "workspace-prs", **state})

    def work():
        last = [0.0]

        def on_progress(p):
            now = time.time()
            # Throttle: "inspect" fires once per workspace and would otherwise
            # push hundreds of events a second down the stream.
            final = p.get("done") and p.get("done") == p.get("total")
            if not final and now - last[0] < _PROGRESS_MIN_INTERVAL:
                _set_workspace_pr_progress(**p)
                return
            last[0] = now
            _publish_event({"type": "workspace-prs", **_set_workspace_pr_progress(**p)})

        try:
            out = _refresh_workspace_prs(db_path, resolver=resolver, on_progress=on_progress)
            _cache_clear()
            state = _set_workspace_pr_progress(
                running=False, phase="done", finished_at=time.time(), stats=out,
                error=out.get("error"))
        except Exception as e:
            state = _set_workspace_pr_progress(
                running=False, phase="error", finished_at=time.time(), error=str(e))
        finally:
            WORKSPACE_PR_LOCK.release()
        _publish_event({"type": "workspace-prs", **state,
                        "workspace_prs": _workspace_pr_status(db_path)})

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, **state}


def _workspace_label(info: dict) -> Optional[str]:
    """Render a cached workspace row for display.

    Rebuilt from the stored parts rather than reusing the stored ``label``
    string, so a change to the label format takes effect on the next page
    load instead of requiring everyone to re-run a network refresh.
    """
    pr = None
    if info.get("pr_number"):
        pr = {"number": info["pr_number"], "title": info.get("pr_title")}
    return pr_links.build_label(
        {"repo": info.get("repo"), "is_main": info.get("is_main"), "branch": info.get("branch")},
        pr,
    )


def _apply_workspace_labels(db_path: str, payload, name_keys=("project_name",),
                            path_key: str = "workspace_path", field_prefix: str = ""):
    """Swap workspace display names for their GitHub PR label, in place.

    A no-op unless the setting is on and the workspace has a cached label, so
    a workspace whose worktree has been deleted (the usual fate of a merged
    branch) keeps the directory-derived name it always had.

    ``payload`` may be a row, a list of rows, or a dict of lists; it is
    returned unchanged so callers can wrap a query result inline.
    """
    if not workspace_pr_links_enabled(db_path):
        return payload
    labels = workspace_pr_map(db_path)
    if not labels:
        return payload

    def decorate(row):
        if not isinstance(row, dict):
            return
        info = labels.get(row.get(path_key))
        if not info:
            return
        label = _workspace_label(info)
        if not label:
            return
        for key in name_keys:
            if key in row and row[key]:
                row.setdefault(f"{key}_original", row[key])
                row[key] = label
        # Prefixed because a row can name two workspaces (cross-workspace
        # leaks name a source and a target); unprefixed they'd overwrite.
        for field in ("pr_number", "pr_title", "pr_url", "pr_state", "repo"):
            row[f"{field_prefix}{field}"] = info.get(field)

    if isinstance(payload, list):
        for row in payload:
            decorate(row)
    elif isinstance(payload, dict):
        decorate(payload)
    return payload


def _apply_sankey_labels(db_path: str, matrix: dict) -> dict:
    """Relabel Sankey nodes, keeping the ' (agent)'/' (files)' suffixes.

    Node names are also link endpoints, so both sides have to be rewritten
    with the same mapping or ECharts drops the links.

    Relabelling is many-to-one — several worktrees can carry the same PR, and
    every worktree of a repo shares one "main worktree" label — so distinct
    nodes collapse onto one name. ECharts' Sankey requires unique node names
    and throws on duplicates, so merged nodes and their links are combined.
    """
    if not workspace_pr_links_enabled(db_path) or not isinstance(matrix, dict):
        return matrix
    labels = workspace_pr_map(db_path)
    if not labels:
        return matrix
    rename: dict = {}
    for node in matrix.get("nodes") or []:
        info = labels.get(node.get("workspace_path"))
        label = _workspace_label(info) if info else None
        if not label:
            continue
        name = node.get("name") or ""
        for suffix in (" (agent)", " (files)"):
            if name.endswith(suffix):
                new = f"{label}{suffix}"
                break
        else:
            new = label
        if new != name:
            rename[name] = new
            node["name_original"] = name
            node["name"] = new

    if rename:
        # Merge nodes that now share a name, keeping every path they stand for
        # so the tooltip can still say which directories are behind the label.
        merged: dict = {}
        for node in matrix.get("nodes") or []:
            name = node.get("name")
            keep = merged.get(name)
            if keep is None:
                node["workspace_paths"] = [p for p in [node.get("workspace_path")] if p]
                merged[name] = node
                continue
            path = node.get("workspace_path")
            if path and path not in keep["workspace_paths"]:
                keep["workspace_paths"].append(path)
        matrix["nodes"] = list(merged.values())

        for link in matrix.get("links") or []:
            if link.get("source") in rename:
                link["source"] = rename[link["source"]]
            if link.get("target") in rename:
                link["target"] = rename[link["target"]]
        # Two links can now join the same pair; sum them into one ribbon.
        combined: dict = {}
        for link in matrix.get("links") or []:
            key = (link.get("source"), link.get("target"))
            if key in combined:
                combined[key]["value"] = (combined[key].get("value") or 0) + (link.get("value") or 0)
            else:
                combined[key] = link
        matrix["links"] = list(combined.values())
    return matrix


WORKSPACE_PR_SETTING = "workspace_pr_links"


def workspace_pr_links_enabled(db_path: str) -> bool:
    """Whether to relabel workspaces with their GitHub PR. Off by default."""
    return get_setting(db_path, WORKSPACE_PR_SETTING, "0") == "1"


def _workspace_pr_status(db_path: str) -> dict:
    rows = workspace_pr_map(db_path)
    checked = [r.get("checked_at") for r in rows.values() if r.get("checked_at")]
    return {
        "enabled": workspace_pr_links_enabled(db_path),
        "linked": len(rows),
        "with_pr": sum(1 for r in rows.values() if r.get("pr_number")),
        "last_checked": max(checked) if checked else None,
        "gh_available": bool(pr_links.find_gh()),
        "git_available": bool(pr_links.find_git()),
    }


def _settings_payload(db_path: str, projects_override: Optional[str] = None) -> dict:
    claude_dir = _claude_dir(db_path)
    projects_dir = _projects_dir(db_path, projects_override)
    return {
        "claude_dir": str(claude_dir),
        "projects_dir": str(projects_dir),
        "projects_overridden": bool(projects_override),
        "claude_dirs": _claude_dirs(db_path),
        "workspace_prs": _workspace_pr_status(db_path),
    }


def build_handler(db_path: str, projects_dir: Optional[str] = None):
    pricing = load_pricing(PRICING_JSON)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_HEAD(self):
            return self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            qs = parse_qs(url.query or "")
            path = url.path
            since = qs.get("since", [None])[0]
            until = qs.get("until", [None])[0]
            cache_key = self.path  # includes query string
            if path in ("/", "/index.html"):
                return _serve_static(self, "index.html")
            if path.startswith("/web/"):
                return _serve_static(self, path[5:])
            if path == "/api/overview-bundle":
                bundle_key = _bundle_cache_key(since, until)
                cached = _cache_get(bundle_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = _overview_bundle(db_path, since, until, pricing)
                _cache_set(bundle_key, data)
                return _send_json(self, data)
            if path == "/api/overview":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                totals = overview_totals(db_path, since, until)
                cost_usd = 0.0
                for m in model_breakdown(db_path, since, until):
                    c = cost_for(m["model"], m, pricing)
                    if c["usd"] is not None:
                        cost_usd += c["usd"]
                totals["cost_usd"] = round(cost_usd, 4)
                _cache_set(cache_key, totals)
                return _send_json(self, totals)
            if path == "/api/prompts":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                sort = qs.get("sort", ["tokens"])[0]
                rows = expensive_prompts(db_path, limit=limit, sort=sort)
                for r in rows:
                    c = cost_for(r["model"], {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_read_tokens": r["cache_read_tokens"],
                        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
                    }, pricing)
                    r["estimated_cost_usd"] = c["usd"]
                _cache_set(cache_key, rows)
                return _send_json(self, rows)
            if path == "/api/projects":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = _apply_workspace_labels(db_path, project_summary(db_path, since, until))
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/tools":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = tool_token_breakdown(db_path, since, until)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/sessions":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = _apply_workspace_labels(db_path, recent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                ))
                by_model = session_model_tokens(db_path, [s["session_id"] for s in data])
                for s in data:
                    total = 0.0
                    estimated = False
                    matched = False
                    for mt in by_model.get(s["session_id"], []):
                        c = cost_for(mt["model"], mt, pricing)
                        if c["usd"] is not None:
                            total += c["usd"]
                            matched = True
                            estimated = estimated or c["estimated"]
                        else:
                            estimated = True
                    s["cost_usd"] = round(total, 4) if matched else None
                    s["cost_estimated"] = estimated
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/daily":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = daily_token_breakdown(db_path, since, until)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/skills":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                rows = skill_breakdown(db_path, since, until)
                catalog = cached_catalog(db_path)
                # Lazy import so deleting skill_budgets.py keeps the server bootable.
                from .skill_budgets import (
                    budget_for,
                    skill_actuals,
                    skill_costs,
                    skill_subagent_costs,
                )
                actuals = skill_actuals(db_path, since, until)
                costs = skill_costs(db_path, pricing, since, until)
                sub = skill_subagent_costs(db_path, pricing, since, until)
                for r in rows:
                    info = catalog.get(r["skill"])
                    r["tokens_per_call"] = info["tokens"] if info else None
                    r["description"] = info["description"] if info else ""
                    r["budget_output_tokens"] = budget_for(r["skill"], catalog)
                    a = actuals.get(r["skill"])
                    r["p50_output_tokens"] = a["p50"] if a else None
                    r["p95_output_tokens"] = a["p95"] if a else None
                    r["over_budget"] = bool(
                        r["budget_output_tokens"]
                        and a
                        and a["p50"] > r["budget_output_tokens"] * 1.2
                    )
                    c = costs.get(r["skill"])
                    r["total_cost_usd"] = c["cost_usd"] if c else None
                    r["cost_estimated"] = bool(c and c["cost_estimated"])
                    s = sub.get(r["skill"])
                    r["subagent_cost_usd"] = s["cost_usd"] if s else None
                    r["subagent_output_tokens"] = s["output_tokens"] if s else 0
                    r["total_with_subagents_usd"] = (
                        (r["total_cost_usd"] or 0.0) + (r["subagent_cost_usd"] or 0.0)
                        if (r["total_cost_usd"] is not None or r["subagent_cost_usd"] is not None)
                        else None
                    )
                    if s and s["cost_estimated"]:
                        r["cost_estimated"] = True
                _cache_set(cache_key, rows)
                return _send_json(self, rows)
            if path == "/api/by-model":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                rows = model_breakdown(db_path, since, until)
                for r in rows:
                    c = cost_for(r["model"], r, pricing)
                    r["cost_usd"] = c["usd"]
                    r["cost_estimated"] = c["estimated"]
                _cache_set(cache_key, rows)
                return _send_json(self, rows)
            if path.startswith("/api/sessions/"):
                sid = path.rsplit("/", 1)[1]
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = _apply_workspace_labels(db_path, session_turns(db_path, sid))
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/workspaces":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = _apply_sankey_labels(db_path, workspaces_matrix(db_path, since, until))
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/cross-workspace-leaks":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = cross_workspace_leaks(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                )
                # Each leak row names two workspaces, so relabel both sides.
                _apply_workspace_labels(db_path, data, ("source",), "source_path", "source_")
                _apply_workspace_labels(db_path, data, ("target",), "target_path", "target_")
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/subagents":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                rows = subagent_breakdown(db_path, since, until)
                for r in rows:
                    c = cost_for(r["model"], r, pricing)
                    r["cost_usd"] = c["usd"]
                    r["cost_estimated"] = c["estimated"]
                top = _apply_workspace_labels(db_path, top_subagent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                ))
                orch = orchestration_breakdown(db_path, since, until)
                _apply_workspace_labels(db_path, orch.get("sdk_runs") or [], ("workspace",))
                for bucket in ("by_kind", "by_entrypoint"):
                    for r in orch[bucket]:
                        c = cost_for(r["model"], r, pricing)
                        r["cost_usd"] = c["usd"]
                        r["cost_estimated"] = c["estimated"]
                tree = _apply_workspace_labels(db_path, dispatch_tree(
                    db_path, limit=_clamp_limit(qs.get("limit", ["50"])[0], 50),
                    since=since, until=until,
                ))
                for r in tree:
                    child_models = r["models"] or []
                    if child_models:
                        c = cost_for(child_models[0], r, pricing)
                        r["child_cost_usd"] = c["usd"]
                        r["child_cost_estimated"] = c["estimated"]
                    else:
                        r["child_cost_usd"] = None
                        r["child_cost_estimated"] = True
                data = {
                    "breakdown": rows,
                    "top_sessions": top,
                    "by_kind": orch["by_kind"],
                    "by_entrypoint": orch["by_entrypoint"],
                    "sdk_runs": orch["sdk_runs"],
                    "dispatch_tree": tree,
                }
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/tips":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                data = all_tips(db_path)
                _cache_set(cache_key, data)
                return _send_json(self, data)
            if path == "/api/plan":
                return _send_json(self, {"plan": get_plan(db_path), "pricing": pricing})
            if path == "/api/plugins":
                return _send_json(self, cached_plugins())
            if path == "/api/mcp":
                cached = _cache_get(cache_key)
                if cached is not None:
                    return _send_json(self, cached)
                rows = scan_mcp()
                tools = tool_token_breakdown(db_path, since, until)
                for r in rows:
                    r["usage_calls"] = _mcp_usage_calls(r["name"], tools)
                _cache_set(cache_key, rows)
                return _send_json(self, rows)
            if path == "/api/hooks":
                return _send_json(self, scan_hooks())
            if path == "/api/commands":
                return _send_json(self, scan_commands())
            if path == "/api/agents":
                return _send_json(self, scan_agents())
            if path == "/api/workspace-prs/status":
                return _send_json(self, {**workspace_pr_progress(),
                                         "workspace_prs": _workspace_pr_status(db_path)})
            if path == "/api/settings":
                return _send_json(self, _settings_payload(db_path, projects_dir))
            if path == "/api/scan":
                with SCAN_LOCK:
                    n = scan_dir(_projects_dir(db_path, projects_dir), db_path)
                return _send_json(self, n)
            if path == "/api/rtk":
                return _send_json(self, _rtk_payload())
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = _subscribe()
                try:
                    while True:
                        try:
                            evt = q.get(timeout=15)
                            chunk = f"data: {json.dumps(evt, default=str)}\n\n".encode()
                        except queue.Empty:
                            chunk = b": ping\n\n"
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                finally:
                    _unsubscribe(q)
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            url = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return _send_error(self, 400, "invalid Content-Length")
            if length < 0 or length > MAX_POST_BYTES:
                return _send_error(self, 413, f"body too large (max {MAX_POST_BYTES} bytes)")
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                return _send_error(self, 400, "invalid JSON")
            if not isinstance(body, dict):
                return _send_error(self, 400, "body must be a JSON object")
            try:
                if url.path == "/api/plan":
                    set_plan(db_path, body.get("plan", "api"))
                    _cache_clear()
                    return _send_json(self, {"ok": True})
                if url.path == "/api/settings":
                    if "plan" in body:
                        set_plan(db_path, body.get("plan", "api"))
                    if "claude_dir" in body:
                        claude_dir, err = _validate_claude_dir(body.get("claude_dir"))
                        if err:
                            return _send_error(self, 400, err)
                        with SCAN_LOCK:
                            set_setting(db_path, "claude_dir", str(claude_dir))
                            _remember_claude_dir(db_path, claude_dir)
                            if body.get("reset_scan_data") is True:
                                clear_scan_data(db_path)
                    _cache_clear()
                    return _send_json(self, {"ok": True, **_settings_payload(db_path, projects_dir)})
                if url.path == "/api/workspace-prs":
                    enabled = bool(body.get("enabled"))
                    set_setting(db_path, WORKSPACE_PR_SETTING, "1" if enabled else "0")
                    if not enabled and body.get("clear"):
                        clear_workspace_prs(db_path)
                    _cache_clear()  # cached payloads carry the old labels
                    result = {"ok": True}
                    if enabled and body.get("refresh", True):
                        result.update(_refresh_workspace_prs_async(db_path))
                    return _send_json(self, {**result, "workspace_prs": _workspace_pr_status(db_path)})
                if url.path == "/api/workspace-prs/refresh":
                    if not workspace_pr_links_enabled(db_path):
                        return _send_error(self, 400, "workspace PR links are turned off")
                    started = _refresh_workspace_prs_async(db_path)
                    # 202: the work runs in the background and reports over SSE.
                    return _send_json(self, {"ok": True, **started},
                                      status=202 if started.get("started") else 409)
                if url.path == "/api/tips/dismiss":
                    dismiss_tip(db_path, body.get("key", ""))
                    _cache_clear()
                    return _send_json(self, {"ok": True})
                if url.path == "/api/refresh":
                    threading.Thread(
                        target=_do_refresh, args=(db_path, projects_dir, pricing), daemon=True
                    ).start()
                    return _send_json(self, {"ok": True})
            except Exception as e:
                return _send_error(self, 503, str(e))
            self.send_response(404)
            self.end_headers()

    return H


def _scan_loop(db_path: str, projects_dir: Optional[str] = None, interval: float = 60.0):
    # Sleep first: avoid contending with the synchronous boot warm + the user's
    # first request. The boot warm covers initial cache population.
    time.sleep(interval)
    while True:
        try:
            if SCAN_LOCK.acquire(blocking=False):
                try:
                    n = scan_dir(_projects_dir(db_path, projects_dir), db_path)
                    # Top up PR links for workspaces that appeared (or had a PR
                    # opened) since the last pass. A no-op when nothing is
                    # missing, so the steady state costs no network calls.
                    topped = _top_up_workspace_prs(db_path)
                    if n["messages"] > 0 or topped.get("with_pr"):
                        _cache_clear()
                    # Emit the event even when messages == 0 so the frontend's
                    # "Getting latest data…" banner clears once the scan finishes.
                    # The frontend uses n.messages to decide whether to flag new data.
                    _publish_event({"type": "scan", "n": n, "ts": time.time()})
                finally:
                    SCAN_LOCK.release()
        except Exception as e:
            _publish_event({"type": "error", "message": str(e)})
        time.sleep(interval)


def run(host: str, port: int, db_path: str, projects_dir: Optional[str] = None):
    pricing = load_pricing(PRICING_JSON)
    # Warm the default range (30d) synchronously before opening the port so
    # the user's first paint is a cache hit. Then warm 7d/90d/all in the
    # background while the server is already serving.
    _warm_one(db_path, pricing, _WARM_DEFAULT_DAYS)
    def _warm_rest():
        for days in _WARM_DAYS:
            if days != _WARM_DEFAULT_DAYS:
                _warm_one(db_path, pricing, days)
    threading.Thread(target=_warm_rest, daemon=True).start()
    threading.Thread(target=_scan_loop, args=(db_path, projects_dir), daemon=True).start()
    H = build_handler(db_path, projects_dir)
    httpd = http.server.ThreadingHTTPServer((host, port), H)
    httpd.serve_forever()
