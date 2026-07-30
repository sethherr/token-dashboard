import { api, fmt, makeSortable, cacheGet, cacheSet, workspaceLabel, bindWorkspaceTooltips } from '/web/app.js';
import { BY_REPO, BY_PROJECT, applyGrouping, hasRepos, readGroup, groupToggle, bindGroupToggle } from '/web/grouping.js';

const URL = '/api/projects';

// This page is the per-workspace list; repository is the roll-up you opt into.
const DEFAULT_GROUP = BY_PROJECT;

function writeGroup(mode) {
  // Hardcoded base — see workspaces.js for the rationale.
  location.hash = '#/projects' + (mode === DEFAULT_GROUP ? '' : '?group=' + encodeURIComponent(mode));
}

export default async function (root) {
  const group = readGroup(DEFAULT_GROUP);
  const cached = cacheGet(URL);
  if (cached) { renderProjects(root, cached, group); return; }

  const fresh = await api(URL);
  cacheSet(URL, fresh);
  renderProjects(root, fresh, group);
}

function renderProjects(root, allRows, group) {
  // Repository comes from the workspace→PR association; with it off every row
  // would collapse into one bucket, so neither the view nor the toggle appear.
  const repoViewAvailable = hasRepos(allRows);
  const mode = repoViewAvailable ? group : BY_PROJECT;
  const rows = applyGrouping(allRows, mode);
  const byRepo = mode === BY_REPO;

  root.innerHTML = `
    <div class="card">
      <div class="flex" style="align-items:baseline;gap:10px">
        <h2 style="margin:0">${byRepo ? 'Repositories' : 'Projects'}</h2>
        <span class="spacer"></span>
        ${repoViewAvailable ? groupToggle(mode, 'proj-group') : ''}
      </div>
      <p class="muted" style="margin:6px 0 14px">Click any column header to sort. Cache reads are billed cheaper, so high cache-read columns are good.</p>
      <table id="projects-table">
        <thead><tr>
          <th>${byRepo ? 'repository' : 'project'}</th>
          ${byRepo ? '<th class="num">workspaces</th>' : ''}
          <th class="num">sessions</th><th class="num">turns</th>
          <th class="num">billable tokens</th><th class="num">cache reads</th>
        </tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td class="blur-sensitive" data-val="${fmt.htmlSafe(r.project_name || r.project_slug)}">${
                byRepo
                  ? fmt.htmlSafe(r.project_name)
                  : workspaceLabel(r.project_name || r.project_slug, r.workspace_path, { prNumber: r.pr_number, prUrl: r.pr_url })
              }</td>
              ${byRepo ? `<td class="num" data-val="${r.workspace_count || 0}">${fmt.int(r.workspace_count)}</td>` : ''}
              <td class="num" data-val="${r.sessions || 0}">${fmt.int(r.sessions)}</td>
              <td class="num" data-val="${r.turns || 0}">${fmt.int(r.turns)}</td>
              <td class="num" data-val="${r.billable_tokens || 0}">${fmt.int(r.billable_tokens)}</td>
              <td class="num" data-val="${r.cache_read_tokens || 0}">${fmt.int(r.cache_read_tokens)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  // Billable tokens sits one column further right when the workspace count shows.
  makeSortable(root.querySelector('#projects-table'), { col: byRepo ? 4 : 3, dir: 'desc' });
  bindGroupToggle(root, 'proj-group', writeGroup);
  bindWorkspaceTooltips(root);
}
