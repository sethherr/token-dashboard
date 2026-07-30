// grouping.js — project rows viewed by workspace or by repository.
//
// A repository's work is spread across many workspaces (one worktree per PR),
// so "which repo costs the most" is invisible in a per-workspace list. These
// helpers roll project rows up by repo and render the toggle that switches
// between the two views.
//
// Repository only exists once the workspace→PR association is on, so callers
// must hide the toggle when `hasRepos()` is false rather than offering a view
// that would show one undifferentiated bucket.
import { fmt } from '/web/app.js';

export const BY_PROJECT = 'project';
export const BY_REPO = 'repo';

const NO_REPO = '(no repository)';

/** Whether any row carries a repository — i.e. whether the repo view is meaningful. */
export function hasRepos(rows) {
  return (rows || []).some(r => r && r.repo);
}

/** Read the ?group= parameter out of the current hash. */
export function readGroup(fallback = BY_PROJECT) {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)group=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return k === BY_REPO || k === BY_PROJECT ? k : fallback;
}

const SUM_FIELDS = [
  'sessions', 'turns', 'input_tokens', 'output_tokens',
  'billable_tokens', 'cache_read_tokens',
];

/**
 * Roll project rows up by repository, preserving the sort order of the input
 * (rows arrive sorted by billable tokens; groups are re-sorted the same way).
 * Rows with no repository collapse into one bucket rather than a long tail.
 */
export function groupByRepo(rows) {
  const out = new Map();
  for (const r of rows || []) {
    const key = r.repo || NO_REPO;
    let g = out.get(key);
    if (!g) {
      g = {
        project_name: key,
        project_slug: key,
        repo: r.repo || null,
        is_group: true,
        workspace_count: 0,
      };
      SUM_FIELDS.forEach(f => { g[f] = 0; });
      out.set(key, g);
    }
    g.workspace_count += 1;
    SUM_FIELDS.forEach(f => { g[f] += r[f] || 0; });
  }
  return [...out.values()].sort((a, b) => (b.billable_tokens || 0) - (a.billable_tokens || 0));
}

/** Apply a grouping mode to project rows. */
export function applyGrouping(rows, mode) {
  return mode === BY_REPO ? groupByRepo(rows) : (rows || []);
}

/**
 * Segmented control markup. Render it in a flex container with
 * `align-items: baseline` so it sits on the heading's baseline.
 */
export function groupToggle(current, id = 'group-toggle') {
  return `<div class="range-tabs" id="${fmt.htmlSafe(id)}" role="group" aria-label="Group by">
    <button type="button" data-group="${BY_REPO}" class="${current === BY_REPO ? 'active' : ''}">repository</button>
    <button type="button" data-group="${BY_PROJECT}" class="${current === BY_PROJECT ? 'active' : ''}">project</button>
  </div>`;
}

/** Wire the toggle. `onPick` receives the chosen mode. */
export function bindGroupToggle(root, id, onPick) {
  root.querySelectorAll(`#${id} button[data-group]`).forEach(btn => {
    btn.addEventListener('click', () => onPick(btn.dataset.group));
  });
}
