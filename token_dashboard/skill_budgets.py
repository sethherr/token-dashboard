"""Skill budget-vs-actual tracking.

Parses user-declared output-token budgets from SKILL.md body text and
measures each skill's actual output-token footprint per invocation.

Two declaration formats are supported (no frontmatter field exists across
the catalog today):
  1. Inline:   ``Execute these steps in order. Complete in <N,NNN output tokens.``
  2. Section:  ``## Token Budget\n< N output tokens.``

Attribution window for "actual": sum of ``output_tokens`` across assistant
messages after this Skill tool_call and before the next Skill call in the
same session (or end of session). Starting another skill naturally
terminates the previous skill's accounting.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .db import connect


_INLINE = re.compile(r"Complete in\s*<\s*([\d,]+)\s+output\s+tokens", re.I)
_SECTION = re.compile(
    r"^##\s*Token\s+Budget\s*$[\r\n]+\s*<\s*([\d,]+)\s+output\s+tokens",
    re.I | re.M,
)


def parse_budget_from_text(text: str) -> Optional[int]:
    """Return declared output-token budget, or None if nothing parsed.

    Inline form wins if both patterns appear in the same file (in the
    sampled corpus they are mutually exclusive, but the inline line sits
    at the top and is the more prescriptive form).
    """
    for rx in (_INLINE, _SECTION):
        m = rx.search(text)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


_budget_cache: dict[tuple[str, float], Optional[int]] = {}


def budget_for(slug: str, catalog=None) -> Optional[int]:
    """Look up a skill's declared budget via the catalog, cache by (path, mtime).

    Missing slug, unreadable file, or unparsed body → None. No exceptions.
    """
    from .skills import cached_catalog

    cat = catalog if catalog is not None else cached_catalog()
    info = cat.get(slug)
    if not info:
        return None
    path = Path(info["path"])
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = (info["path"], mtime)
    if key in _budget_cache:
        return _budget_cache[key]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _budget_cache[key] = None
        return None
    val = parse_budget_from_text(text)
    _budget_cache[key] = val
    return val


def _range_clause(since, until):
    where, args = [], []
    if since:
        where.append("timestamp >= ?")
        args.append(since)
    if until:
        where.append("timestamp < ?")
        args.append(until)
    return ((" AND " + " AND ".join(where)) if where else "", args)


def _percentile(sorted_xs: list[int], p: int) -> int:
    if not sorted_xs:
        return 0
    k = (len(sorted_xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_xs) - 1)
    if lo == hi:
        return sorted_xs[lo]
    return int(sorted_xs[lo] * (hi - k) + sorted_xs[hi] * (k - lo))


# User-role messages that are system-injected (not real typing) and must not
# terminate the attribution window. Skill/agent invocations inject the body
# of SKILL.md/AGENT.md as a 20k+ user-role message; other Claude Code
# machinery injects the bracketed tags below. Empirically this covers ~25%
# of non-empty user messages; the other ~75% are the user actually typing.
_META_USER_PREFIXES = (
    "Base directory for this skill:",
    "Base directory for this agent:",
    "<system-reminder>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "[Request interrupted",
)


def skill_costs(db_path, pricing, since=None, until=None) -> dict[str, dict]:
    """Return ``{slug: {cost_usd, cost_estimated}}`` — total assistant-side
    cost spent within each skill's attribution window.

    Uses the same window-bounds logic as ``skill_actuals`` (exclude sidechain,
    cap at next Skill or next real-user-typed message). Groups tokens by
    (skill, model) so each bucket is priced with the matching model's rate;
    falls back to tier-level pricing for unknown-but-named models.

    Answers "which skill actually costs the most dollars" — a direct
    ranking signal that the p50/p95 output view doesn't surface (a skill
    with small p50 but many invocations can still dominate monthly spend).
    """
    from .pricing import cost_for as _cost_for

    rng, args = _range_clause(since, until)
    not_like = " AND ".join(
        ["u.prompt_text NOT LIKE ?"] * len(_META_USER_PREFIXES)
    )
    like_args = [p + "%" for p in _META_USER_PREFIXES]
    sql = f"""
      WITH calls AS (
        SELECT session_id,
               target     AS skill,
               timestamp  AS start_ts,
               LEAD(timestamp) OVER (
                 PARTITION BY session_id ORDER BY timestamp
               ) AS next_skill_ts
          FROM tool_calls
         WHERE tool_name = 'Skill'
           AND target IS NOT NULL
           AND target != ''
           {rng}
      ),
      bounds AS (
        SELECT c.session_id, c.skill, c.start_ts, c.next_skill_ts,
               (SELECT MIN(u.timestamp) FROM messages u
                 WHERE u.session_id   = c.session_id
                   AND u.type         = 'user'
                   AND u.is_sidechain = 0
                   AND u.prompt_chars IS NOT NULL
                   AND u.prompt_chars > 0
                   AND u.prompt_text  IS NOT NULL
                   AND u.timestamp    > c.start_ts
                   AND {not_like}
               ) AS next_user_ts
          FROM calls c
      )
      SELECT b.skill,
             COALESCE(m.model, 'unknown')                    AS model,
             COALESCE(SUM(m.input_tokens), 0)                AS input_tokens,
             COALESCE(SUM(m.output_tokens), 0)               AS output_tokens,
             COALESCE(SUM(m.cache_read_tokens), 0)           AS cache_read_tokens,
             COALESCE(SUM(m.cache_create_5m_tokens), 0)      AS cache_create_5m_tokens,
             COALESCE(SUM(m.cache_create_1h_tokens), 0)      AS cache_create_1h_tokens
        FROM bounds b
        LEFT JOIN messages m
          ON m.session_id   = b.session_id
         AND m.type         = 'assistant'
         AND m.is_sidechain = 0
         AND m.timestamp    > b.start_ts
         AND (b.next_skill_ts IS NULL OR m.timestamp < b.next_skill_ts)
         AND (b.next_user_ts  IS NULL OR m.timestamp < b.next_user_ts)
       GROUP BY b.skill, m.model
    """
    args = [*args, *like_args]
    out: dict[str, dict] = {}
    with connect(db_path) as conn:
        for row in conn.execute(sql, args):
            usage = {
                "input_tokens":           row["input_tokens"],
                "output_tokens":          row["output_tokens"],
                "cache_read_tokens":      row["cache_read_tokens"],
                "cache_create_5m_tokens": row["cache_create_5m_tokens"],
                "cache_create_1h_tokens": row["cache_create_1h_tokens"],
            }
            c = _cost_for(row["model"] or "", usage, pricing)
            entry = out.setdefault(row["skill"], {"cost_usd": 0.0, "cost_estimated": False})
            if c["usd"] is None:
                # Model not in pricing and no tier match: flag estimated, skip add.
                entry["cost_estimated"] = True
                continue
            entry["cost_usd"] += c["usd"]
            if c["estimated"]:
                entry["cost_estimated"] = True
    for v in out.values():
        v["cost_usd"] = round(v["cost_usd"], 4)
    return out


def skill_actuals(db_path, since=None, until=None) -> dict[str, dict]:
    """Return ``{slug: {p50, p95, count}}`` of output_tokens per invocation.

    Window boundaries, in priority order:
      1. next Skill call in the same session,
      2. next real-user-typed main-chain message (``prompt_chars > 0`` and
         ``prompt_text`` does NOT start with any system-injection prefix),
      3. end of session.

    Sidechain assistant output (subagents, auto-compaction) is excluded —
    it is not emitted by the skill itself and would otherwise leak in when
    an auto-compact agent fires during the window.

    Note on what ``output_tokens`` counts: the Anthropic API ``output_tokens``
    field includes tool_use JSON blocks and thinking blocks, not just
    user-visible text. Skills that declare "Complete in <N output tokens"
    usually mean text-only output, so a 2-5× gap between declared budget
    and measured p50 can reflect tool_use overhead rather than a bloated
    skill.
    """
    rng, args = _range_clause(since, until)
    # Build "m.prompt_text NOT LIKE ?" chain for the meta-prefix filter.
    not_like = " AND ".join(
        ["u.prompt_text NOT LIKE ?"] * len(_META_USER_PREFIXES)
    )
    like_args = [p + "%" for p in _META_USER_PREFIXES]
    sql = f"""
      WITH calls AS (
        SELECT session_id,
               target     AS skill,
               timestamp  AS start_ts,
               LEAD(timestamp) OVER (
                 PARTITION BY session_id ORDER BY timestamp
               ) AS next_skill_ts
          FROM tool_calls
         WHERE tool_name = 'Skill'
           AND target IS NOT NULL
           AND target != ''
           {rng}
      ),
      bounds AS (
        SELECT c.session_id, c.skill, c.start_ts, c.next_skill_ts,
               (SELECT MIN(u.timestamp) FROM messages u
                 WHERE u.session_id   = c.session_id
                   AND u.type         = 'user'
                   AND u.is_sidechain = 0
                   AND u.prompt_chars IS NOT NULL
                   AND u.prompt_chars > 0
                   AND u.prompt_text  IS NOT NULL
                   AND u.timestamp    > c.start_ts
                   AND {not_like}
               ) AS next_user_ts
          FROM calls c
      )
      SELECT b.skill,
             COALESCE(SUM(m.output_tokens), 0) AS output_tokens
        FROM bounds b
        LEFT JOIN messages m
          ON m.session_id   = b.session_id
         AND m.type         = 'assistant'
         AND m.is_sidechain = 0
         AND m.timestamp    > b.start_ts
         AND (b.next_skill_ts IS NULL OR m.timestamp < b.next_skill_ts)
         AND (b.next_user_ts  IS NULL OR m.timestamp < b.next_user_ts)
       GROUP BY b.skill, b.session_id, b.start_ts
    """
    args = [*args, *like_args]
    samples: dict[str, list[int]] = {}
    with connect(db_path) as conn:
        for row in conn.execute(sql, args):
            samples.setdefault(row["skill"], []).append(row["output_tokens"] or 0)
    out: dict[str, dict] = {}
    for slug, xs in samples.items():
        xs.sort()
        out[slug] = {
            "p50":   _percentile(xs, 50),
            "p95":   _percentile(xs, 95),
            "count": len(xs),
        }
    return out


def skill_subagent_costs(db_path, pricing, since=None, until=None) -> dict[str, dict]:
    """Return ``{slug: {cost_usd, cost_estimated, output_tokens}}`` — total
    assistant-side cost of all subagent (sidechain) work dispatched by each
    skill within its attribution window.

    A subagent is any sidechain conversation with its own ``agent_id`` (the
    hash in ``subagents/agent-<hash>.jsonl``) except auto-compaction
    (``agent_id LIKE 'acompact%'``). Attribution ties each subagent to the
    skill window that contains its FIRST sidechain message.

    Window bounds: Skill call → next Skill call in session. This differs
    from ``skill_costs``'s window, which ALSO closes at the first real
    user-typed message — some interactive orchestrators ask the user a
    question mid-execution and THEN dispatch subagents, so closing at
    the user's reply would miss the whole point. The next Skill call
    remains the correct boundary for "when did this orchestrator hand
    off to a new one."

    Nested subagents fall out naturally: the inner subagent's first
    sidechain message is itself inside the outer skill's window, so the
    inner agent_id attributes to the same orchestrator.
    """
    from .pricing import cost_for as _cost_for

    rng, args = _range_clause(since, until)
    sql = f"""
      WITH calls AS (
        SELECT session_id,
               target     AS skill,
               timestamp  AS start_ts,
               LEAD(timestamp) OVER (
                 PARTITION BY session_id ORDER BY timestamp
               ) AS next_skill_ts
          FROM tool_calls
         WHERE tool_name = 'Skill'
           AND target IS NOT NULL
           AND target != ''
           {rng}
      ),
      -- Each subagent is identified by (session_id, agent_id). Its "start"
      -- is the earliest sidechain message with that agent_id. Auto-compaction
      -- agents are excluded by the acompact%-prefix filter.
      agent_starts AS (
        SELECT session_id, agent_id, MIN(timestamp) AS start_ts
          FROM messages
         WHERE is_sidechain = 1
           AND agent_id IS NOT NULL
           AND agent_id NOT LIKE 'acompact%'
         GROUP BY session_id, agent_id
      ),
      window_agents AS (
        SELECT c.skill, c.session_id, a.agent_id
          FROM calls c
          JOIN agent_starts a
            ON a.session_id = c.session_id
           AND a.start_ts  > c.start_ts
           AND (c.next_skill_ts IS NULL OR a.start_ts < c.next_skill_ts)
      )
      SELECT w.skill,
             COALESCE(m.model, 'unknown')                    AS model,
             COALESCE(SUM(m.input_tokens), 0)                AS input_tokens,
             COALESCE(SUM(m.output_tokens), 0)               AS output_tokens,
             COALESCE(SUM(m.cache_read_tokens), 0)           AS cache_read_tokens,
             COALESCE(SUM(m.cache_create_5m_tokens), 0)      AS cache_create_5m_tokens,
             COALESCE(SUM(m.cache_create_1h_tokens), 0)      AS cache_create_1h_tokens
        FROM window_agents w
        JOIN messages m
          ON m.session_id   = w.session_id
         AND m.agent_id     = w.agent_id
         AND m.is_sidechain = 1
         AND m.type         = 'assistant'
       GROUP BY w.skill, m.model
    """
    out: dict[str, dict] = {}
    with connect(db_path) as conn:
        for row in conn.execute(sql, args):
            usage = {
                "input_tokens":           row["input_tokens"],
                "output_tokens":          row["output_tokens"],
                "cache_read_tokens":      row["cache_read_tokens"],
                "cache_create_5m_tokens": row["cache_create_5m_tokens"],
                "cache_create_1h_tokens": row["cache_create_1h_tokens"],
            }
            c = _cost_for(row["model"] or "", usage, pricing)
            entry = out.setdefault(row["skill"], {
                "cost_usd": 0.0,
                "cost_estimated": False,
                "output_tokens": 0,
            })
            entry["output_tokens"] += row["output_tokens"] or 0
            if c["usd"] is None:
                entry["cost_estimated"] = True
                continue
            entry["cost_usd"] += c["usd"]
            if c["estimated"]:
                entry["cost_estimated"] = True
    for v in out.values():
        v["cost_usd"] = round(v["cost_usd"], 4)
    return out
