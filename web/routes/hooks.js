import { api, fmt } from '/web/app.js';

function section(title, items, renderItem) {
  return `
    <h3 style="margin-top:20px">${title} (${items.length})</h3>
    <div class="row cols-3">
      ${items.map(renderItem).join('')}
    </div>`;
}

export default async function (root) {
  const [hooks, commands, agents] = await Promise.all([
    api('/api/hooks'), api('/api/commands'), api('/api/agents'),
  ]);
  root.innerHTML = `
    <h2 style="margin:0 0 14px;font-size:16px;letter-spacing:-0.01em">Hooks / Commands / Agents</h2>
    ${section('Hooks', hooks, h => `
      <div class="card">
        <h3>${fmt.htmlSafe(h.name)} <span class="badge ${h.status === 'ok' ? 'sonnet' : ''}">${h.status === 'ok' ? 'ok' : 'broken'}</span></h3>
        <p style="font-size:12px">event: ${fmt.htmlSafe(h.event)}</p>
        ${h.file_path ? `<button data-open="${fmt.htmlSafe(h.file_path)}">Open in editor</button>` : ''}
      </div>`)}
    ${section('Commands', commands, c => `
      <div class="card">
        <h3>/${fmt.htmlSafe(c.name)}</h3>
        <p style="font-size:12px">source: ${fmt.htmlSafe(c.source)}</p>
        <button data-open="${fmt.htmlSafe(c.file_path)}">Open in editor</button>
      </div>`)}
    ${section('Agents', agents, a => `
      <div class="card">
        <h3>${fmt.htmlSafe(a.name)}</h3>
        <p style="font-size:12px">source: ${fmt.htmlSafe(a.source)}</p>
        <button data-open="${fmt.htmlSafe(a.file_path)}">Open in editor</button>
      </div>`)}
  `;
  root.querySelectorAll('[data-open]').forEach(btn => {
    btn.addEventListener('click', () => { window.location.href = fmt.editorLink(btn.dataset.open); });
  });
}
