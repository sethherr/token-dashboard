import { api, fmt } from '/web/app.js';

export default async function (root) {
  const servers = await api('/api/mcp');
  root.innerHTML = `
    <div class="flex" style="margin-bottom:6px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">MCP Servers</h2>
      <span class="muted" style="font-size:12px">${servers.length} configured</span>
    </div>
    <p class="muted" style="font-size:12px;margin:0 0 14px">Local servers and those shipped by installed plugins. Account-level claude.ai connectors (Gmail, Calendar, Slack) aren't shown — they have no local config to read.</p>
    <div class="row cols-3">
      ${servers.map(s => `
        <div class="card">
          <h3>${fmt.htmlSafe(s.name)}
            <span class="badge">${fmt.htmlSafe(s.kind)}</span>
          </h3>
          <p style="font-size:12px">source: ${fmt.htmlSafe(s.source)}</p>
          <p style="font-size:12px">status: ${fmt.htmlSafe(s.status || '—')}</p>
          ${s.usage_calls != null ? `<p style="font-size:12px">${fmt.int(s.usage_calls)} calls (all time)</p>` : ''}
          ${s.file_path ? `<button data-open="${fmt.htmlSafe(s.file_path)}">Open config in editor</button>` : ''}
        </div>
      `).join('')}
    </div>
  `;
  root.querySelectorAll('[data-open]').forEach(btn => {
    btn.addEventListener('click', () => { window.location.href = fmt.editorLink(btn.dataset.open); });
  });
}
