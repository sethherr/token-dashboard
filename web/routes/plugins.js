import { api, fmt } from '/web/app.js';

export default async function (root) {
  const plugins = await api('/api/plugins');
  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Plugins</h2>
      <span class="muted" style="font-size:12px">${plugins.length} installed</span>
    </div>
    <div class="row cols-3">
      ${plugins.map(p => `
        <div class="card">
          <h3>${fmt.htmlSafe(p.name)}
            <span class="badge ${p.enabled ? 'sonnet' : ''}">${p.enabled ? 'enabled' : 'disabled'}</span>
          </h3>
          <p class="muted" style="font-size:12px;min-height:32px">${fmt.htmlSafe(p.description || 'no description')}</p>
          <p style="font-size:12px">v${fmt.htmlSafe(p.version)} · source: ${fmt.htmlSafe(p.source)}</p>
          <p style="font-size:12px">${p.components.skills} skills · ${p.components.agents} agents · ${p.components.commands} commands</p>
          <p style="font-size:12px">~${fmt.int(p.components.tokens)} tok always-on <span class="muted">(approx)</span></p>
          ${p.install_path ? `<button data-open="${fmt.htmlSafe(p.install_path)}">Open in editor</button>` : ''}
        </div>
      `).join('')}
    </div>
  `;
  root.querySelectorAll('[data-open]').forEach(btn => {
    btn.addEventListener('click', () => { window.location.href = fmt.editorLink(btn.dataset.open); });
  });
}
