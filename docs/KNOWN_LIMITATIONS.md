# Known Limitations

None of these are blockers — the dashboard still gives you useful information. They're the rough edges you'll notice if you look hard.

## Skills tokens-per-call is blank when a skill runs only through Task/Agent

The Skills route shows every skill Claude Code invoked, how many times, across how many sessions, and when. The **tokens-per-call** column is populated for every skill whose `SKILL.md` lives under `~/.claude/skills/`, `~/.claude/scheduled-tasks/`, an `installPath` listed in `~/.claude/plugins/installed_plugins.json` (the active plugin manifest — marketplace clones that were never installed are deliberately excluded), or a project-local `.claude/skills/` directory discovered from the cwds in your session history. A skill that runs only through the `Task`/`Agent` tool with a skill-shaped `subagent_type` (never as a direct `Skill` invocation) arrives without a resolvable slug on disk and its tokens-per-call stays blank.

Cost attribution for orchestrator skills — any skill that dispatches subagents via `Task`/`Agent` — follows the `parent_uuid` chain from every dispatch back to the skill call that emitted it. The `total inc. subagents` column on the Skills tab reflects that. If you upgraded from an older build and the column looks low, run `python3 cli.py rescan-agent-targets` once to re-parse main-session JSONLs whose Agent rows lost their `subagent_type` target.

## The MCP page lists local and plugin-shipped servers, not claude.ai connectors

The MCP page under **Register** shows the MCP servers it can read from disk: local servers configured in `~/.claude.json` (top-level or project-scoped `mcpServers`) and servers bundled by installed, enabled plugins (their `.mcp.json`). Account-level connectors you've enabled on claude.ai — Gmail, Calendar, Slack and the like — are **not** shown. They have no local config file (the only on-disk trace is a stale, incomplete "ever connected" name list), so there's nothing reliable to scan. The page shows only what it can read, which is why it needs no hand-maintained connector file.

## Register catalogs (Plugins / MCP / Hooks) are manifest-driven

The Plugins, MCP and Hooks/Commands/Agents pages only list plugins recorded in `~/.claude/plugins/installed_plugins.json`. Plugins enabled from a local marketplace *clone* rather than cache-installed through the manifest are excluded — the same rule the skill catalog uses, so counts stay consistent across tabs. Disabled plugins are skipped too. A hook whose `settings.json` command is inline (no script path) or uses a backslash-only Windows path shows up without a resolvable script link.

## The Prompts tab shows your main-thread prompts only

The Prompts route lists each typed prompt and the assistant work it triggered, linked by **session + timestamp window**. Newer Claude Code versions interpose `attachment` records between a prompt and its assistant turn (and put no `promptId` on assistant rows), so the original `parent_uuid` link became unreliable and the tab appeared frozen — see `FORK_NOTES.md`. Two consequences of the window-based linkage:

- **Subagent / `Task` prompts don't appear here.** Only main-thread prompts (`is_sidechain = 0`) are listed; subagent dispatch prompts and their token spend live on the **Subagents** tab instead.
- **`tokens` is the whole turn, and programmatic prompts show up too.** The token figure aggregates every main-thread assistant message from a prompt up to the next one — the full turn including its tool loop, not just the first reply. A transcript can't tell a hand-typed prompt from a programmatic one, so an SDK/agent harness that re-feeds a large instruction block each turn appears as a prompt too, usually with large token counts.

## Cost for Pro / Max / Max-20x users is shown as API-equivalent, not subscription value

The Settings route lets you select your pricing plan, but the Overview cost number is always the API-equivalent (what the same usage would have cost on pay-per-token rates). If you're on Pro you pay a flat $20/month regardless of how much of that API-equivalent number you rack up. We don't do "subscription ROI" math yet — Anthropic doesn't publish per-plan rate limits as public JSON, and faking it would be worse than not doing it.

## Cowork sessions are invisible

If you use Claude's Cowork mode (server-side sessions, not local `claude` CLI), those sessions don't write JSONL to `~/.claude/projects/` and the dashboard can't see them.

## Non-standard model names get tier-fallback pricing

If a transcript references a model ID not in `pricing.json` (e.g. a future snapshot that isn't in our table yet), cost is estimated from the tier substring (`fable` / `opus` / `sonnet` / `haiku`) in the name. The UI marks these as `estimated: true`. If the model name contains none of those substrings, cost is reported as null.

## First scan can be slow

The first `python3 cli.py scan` on a heavy user's machine can read tens of MB across hundreds of JSONLs. Subsequent scans are incremental (mtime + byte-offset tracking in the `files` table), so they're fast.

## Running two dashboards against the same DB

Both will fight over the SQLite file and you'll see inconsistent numbers and occasional `database is locked` errors. Only run one at a time. If you want to view the dashboard from a second device, use `HOST=0.0.0.0` on the one running machine and point the second device's browser at it.

## Workspace PR association only covers worktrees that still exist

The optional *associate workspaces with GitHub PRs* setting resolves a workspace
by running `git` inside its directory. A worktree that has been deleted — the
normal fate of a branch once it merges — can't report its branch, so there's
nothing to match a PR against. Those workspaces keep their directory-derived
name, which is the honest answer: the association is unknown, not absent.

The practical effect on a long transcript history is that most historical
workspaces stay unlabelled and recent, still-checked-out ones get PR titles.
Nothing is lost — labels only ever replace a directory name that is still shown
in the hover/click tooltip.

Two smaller bounds:

- PR lookups hit the network, so a refresh resolves at most 200 of them
  (workspaces are processed busiest-first; the rest still get
  `{repo}: {branch}` from the local checkout). The response reports how many
  were throttled.
- Association is a point-in-time snapshot, refreshed only when you press
  **Refresh PR links** or toggle the setting on. A PR retitled after that shows
  its old title until the next refresh.
