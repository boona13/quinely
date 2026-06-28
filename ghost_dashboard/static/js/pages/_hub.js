/**
 * Shared tabbed-hub helper.
 *
 * Composes existing page renderers as tabs inside a single page, so several
 * related pages can live under one nav entry without rewriting any page logic.
 * Each tab delegates to a normal `render(container)` page module, rendering one
 * tab at a time into a shared sub-container (so per-page `getElementById` calls
 * and module-level state stay conflict-free).
 *
 * config:
 *   storageKey  unique localStorage key remembering the last active tab
 *   tabs        [{ id, label, render, refreshMs? }]
 *   defaultTab  optional id to open first (defaults to the first tab)
 */
export function mountHub(container, { storageKey, tabs, defaultTab }) {
  let active = localStorage.getItem(storageKey) || defaultTab || tabs[0].id;
  if (!tabs.some((t) => t.id === active)) active = tabs[0].id;
  let refreshTimer = null;

  container.innerHTML = `
    <div class="hub-tabbar flex gap-1 mb-4 border-b border-zinc-800 overflow-x-auto">
      ${tabs
        .map(
          (t) =>
            `<button class="evo-tab hub-tab px-4 py-2 text-sm font-medium whitespace-nowrap ${
              t.id === active ? 'active' : ''
            }" data-tab="${t.id}">${t.label}</button>`
        )
        .join('')}
    </div>
    <div id="hub-body"></div>
  `;

  const body = document.getElementById('hub-body');

  async function show(id) {
    const tab = tabs.find((t) => t.id === id) || tabs[0];
    active = tab.id;
    try { localStorage.setItem(storageKey, active); } catch {}

    container.querySelectorAll('.hub-tab').forEach((b) =>
      b.classList.toggle('active', b.dataset.tab === active)
    );

    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }

    try {
      await tab.render(body);
    } catch (err) {
      body.innerHTML = `<div class="text-red-400 p-4">Failed to load: ${err?.message || err}</div>`;
      return;
    }

    if (tab.refreshMs) {
      refreshTimer = setInterval(() => {
        if (!document.body.contains(body)) {
          clearInterval(refreshTimer);
          refreshTimer = null;
          return;
        }
        if (active === tab.id) tab.render(body).catch(() => {});
      }, tab.refreshMs);
    }
  }

  container.querySelectorAll('.hub-tab').forEach((b) =>
    b.addEventListener('click', () => show(b.dataset.tab))
  );

  return show(active);
}
