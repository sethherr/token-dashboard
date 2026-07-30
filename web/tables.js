// tables.js — long tables get a fixed-height scrolling frame plus a floating
// collapse toggle, so a 400-row table doesn't bury the rest of the page.
//
// Applied generically to every <table> the routes render (opt out with a
// `data-no-collapse` attribute). A qualifying table is wrapped as:
//
//   <div class="table-collapsible collapsed scrollable">
//     <div class="table-scroll"><table>…</table></div>
//     <button class="table-collapse-toggle">
//   </div>
//
// While collapsed the frame is ~ROW_LIMIT rows tall, the thead sticks to its
// top and any tfoot sticks to its bottom — so totals stay visible without
// scrolling, and the toggle floats above the footer rather than over it.
// Expanding drops the height cap (and the scrollbar with it); the toggle stays
// put at the bottom of the frame so scrolling can be turned back on.

const ROW_LIMIT = 10;   // rows visible before the frame starts scrolling
const PEEK_PX   = 18;   // sliver of row 11 left showing under the toggle

/** Wrap every long table under `root`, and re-measure ones already wrapped. */
export function enhanceTables(root = document) {
  for (const table of root.querySelectorAll('table')) {
    if (table.dataset.noCollapse != null) continue;
    const shell = table.closest('.table-collapsible');
    if (shell) { sync(shell); continue; }
    if (bodyRows(table).length > ROW_LIMIT) wrap(table);
  }
}

/**
 * Re-run enhanceTables whenever routes swap tables in (drawers, filter
 * re-renders). Only childList is observed, so the class/style writes sync()
 * makes can't feed back into the observer.
 */
export function watchTables(root) {
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; enhanceTables(root); });
  }).observe(root, { childList: true, subtree: true });
}

function bodyRows(table) {
  return [...table.tBodies].flatMap(tb => [...tb.rows]);
}

// offsetParent is null for display:none rows — that's how filtering hides them.
const isVisible = tr => tr.offsetParent !== null;

function wrap(table) {
  const shell = document.createElement('div');
  shell.className = 'table-collapsible collapsed';
  const frame = document.createElement('div');
  frame.className = 'table-scroll';
  table.replaceWith(shell);
  frame.appendChild(table);
  shell.appendChild(frame);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'table-collapse-toggle';
  btn.addEventListener('click', () => {
    const collapsing = !shell.classList.contains('collapsed');
    shell.classList.toggle('collapsed');
    if (collapsing) {
      frame.scrollTop = 0;
      // Collapsing a long table yanks the page up under the reader; if the
      // table now starts off-screen, bring its top back into view.
      if (shell.getBoundingClientRect().top < 0) shell.scrollIntoView({ block: 'start' });
    }
    sync(shell);
  });
  shell.appendChild(btn);

  // Sorting, filtering and re-renders all change the table's height.
  new ResizeObserver(() => sync(shell)).observe(table);
  sync(shell);
}

function sync(shell) {
  const table = shell.querySelector('table');
  const btn   = shell.querySelector(':scope > .table-collapse-toggle');
  if (!table || !btn) return;

  const rows   = bodyRows(table).filter(isVisible);
  const footH  = table.tFoot ? table.tFoot.offsetHeight : 0;
  const worth  = rows.length > ROW_LIMIT;

  shell.classList.toggle('scrollable', worth);
  shell.style.setProperty('--table-foot-h', footH + 'px');
  if (worth) {
    // Measured off bounding rects rather than offsetTop so a scrolled frame,
    // or a positioned ancestor, can't skew it.
    const top = table.getBoundingClientRect().top;
    const cut = rows[ROW_LIMIT - 1].getBoundingClientRect().bottom;
    shell.style.setProperty('--table-collapsed-h', Math.round(cut - top) + PEEK_PX + footH + 'px');
  }

  const collapsed = shell.classList.contains('collapsed');
  const label = collapsed ? `Show all ${rows.length} rows ▾` : 'Collapse ▴';
  if (btn.textContent !== label) btn.textContent = label;   // no-op writes would re-trigger the observer
  btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  btn.title = collapsed ? 'Expand to the full table' : `Scroll within ${ROW_LIMIT} rows`;
}
