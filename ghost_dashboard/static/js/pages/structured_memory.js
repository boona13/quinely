/** Structured Memory page — live-updating context sections, facts, and queue status */

let _pollTimer = null;
let _lastUpdated = null;

export async function render(container) {
  _stopPolling();

  const { GhostAPI: api, GhostUtils: u } = window;

  let status, memData;
  try {
    [status, memData] = await Promise.all([
      api.get('/api/structured-memory/status'),
      api.get('/api/structured-memory/data'),
    ]);
  } catch (e) {
    container.innerHTML = `<div class="text-red-400 p-4">Failed to load structured memory: ${u.escapeHtml(e.message)}</div>`;
    return;
  }

  _lastUpdated = status.lastUpdated;

  const filledSections = Object.values(status.sections || {}).filter(Boolean).length;
  const totalSections = Object.keys(status.sections || {}).length;
  const lastUpdated = status.lastUpdated ? u.timeAgo(status.lastUpdated) : 'Never';

  const categoryBadges = Object.entries(status.facts_by_category || {})
    .map(([cat, count]) => `<span class="badge badge-zinc text-[10px]">${u.escapeHtml(cat)}: ${count}</span>`)
    .join(' ');

  container.innerHTML = `
    <div class="flex items-center justify-between mb-1">
      <h1 class="page-header">Structured Memory</h1>
      <div class="flex gap-2 items-center">
        <button id="sm-refresh" class="btn btn-sm btn-secondary">Refresh</button>
        <span class="badge ${status.enabled ? 'badge-green' : 'badge-zinc'}">${status.enabled ? 'Enabled' : 'Disabled'}</span>
      </div>
    </div>
    <p class="page-desc">Persistent context extracted from conversations — user profile, history, and confidence-scored facts.</p>

    <!-- Live processing banner -->
    <div id="sm-processing-banner" class="mt-4 ${status.queue_processing || status.queue_pending > 0 ? '' : 'hidden'}">
      <div class="flex items-center gap-3 px-4 py-3 rounded-lg border" style="background:rgba(245,158,11,0.06);border-color:rgba(245,158,11,0.15)">
        <div class="animate-spin w-4 h-4 border-2 border-amber-400 border-t-transparent rounded-full flex-shrink-0"></div>
        <div>
          <div class="text-xs font-medium text-amber-300" id="sm-banner-text">${_getBannerText(status)}</div>
          <div class="text-[10px] text-amber-400/60">Page will update automatically when done.</div>
        </div>
      </div>
    </div>

    <!-- Stat cards -->
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 mt-4 mb-6">
      <div class="metric-card-v2">
        <div class="metric-card-icon-wrap bg-ghost-500/10">
          <svg class="w-4 h-4 text-ghost-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
        </div>
        <div>
          <div class="metric-card-label">Last Updated</div>
          <div class="metric-card-value text-sm" id="sm-stat-updated">${u.escapeHtml(lastUpdated)}</div>
        </div>
      </div>
      <div class="metric-card-v2">
        <div class="metric-card-icon-wrap bg-blue-500/10">
          <svg class="w-4 h-4 text-blue-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
        </div>
        <div>
          <div class="metric-card-label">Facts</div>
          <div class="flex items-baseline gap-2">
            <div class="metric-card-value" id="sm-stat-facts">${status.facts_count}</div>
            <div class="metric-card-sub">stored</div>
          </div>
        </div>
      </div>
      <div class="metric-card-v2">
        <div class="metric-card-icon-wrap bg-amber-500/10">
          <svg class="w-4 h-4 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h7"/></svg>
        </div>
        <div>
          <div class="metric-card-label">Sections</div>
          <div class="flex items-baseline gap-2">
            <div class="metric-card-value" id="sm-stat-sections">${filledSections}/${totalSections}</div>
            <div class="metric-card-sub">filled</div>
          </div>
        </div>
      </div>
      <div class="metric-card-v2">
        <div class="metric-card-icon-wrap bg-emerald-500/10">
          <svg class="w-4 h-4 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
        </div>
        <div>
          <div class="metric-card-label">Queue</div>
          <div class="metric-card-value text-sm" id="sm-stat-queue">${_queueBadge(status)}</div>
        </div>
      </div>
    </div>

    <!-- User Context -->
    <div id="sm-section-user">
    ${renderSectionGroup('User Context', memData.user || {}, [
      ['workContext', 'Work Context'],
      ['personalContext', 'Personal Context'],
      ['topOfMind', 'Top of Mind'],
    ], u)}
    </div>

    <!-- History -->
    <div id="sm-section-history">
    ${renderSectionGroup('History', memData.history || {}, [
      ['recentMonths', 'Recent Months'],
      ['earlierContext', 'Earlier Context'],
      ['longTermBackground', 'Long-term Background'],
    ], u)}
    </div>

    <!-- Facts -->
    <div class="mt-6">
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-sm font-semibold text-zinc-400">Facts (<span id="sm-facts-total">${status.facts_count}</span> total)</h2>
        <div class="flex gap-2 items-center" id="sm-category-badges">
          ${categoryBadges}
          <select id="sm-fact-filter" class="form-input text-xs py-1 px-2 bg-surface-800 border-surface-700 text-zinc-300 rounded">
            <option value="">All categories</option>
            ${Object.keys(status.facts_by_category || {}).map(c => `<option value="${u.escapeHtml(c)}">${u.escapeHtml(c)}</option>`).join('')}
          </select>
        </div>
      </div>
      <div id="sm-facts-list" class="space-y-2">
        ${renderFactsList(memData.facts || [], u)}
      </div>
    </div>
  `;

  bindEvents(container, api, u, memData);
  _startPolling(container, api, u, status);
}

function _getBannerText(status) {
  if (status.queue_processing) return 'Memory is being updated by the LLM — extracting facts and context...';
  if (status.queue_pending > 0) return `${status.queue_pending} conversation(s) queued for memory processing...`;
  return '';
}

function _queueBadge(status) {
  if (status.queue_processing) return '<span class="text-amber-400 flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse"></span>Processing</span>';
  if (status.queue_pending > 0) return `<span class="text-blue-400">${status.queue_pending} pending</span>`;
  return '<span class="text-emerald-400">Idle</span>';
}

function _startPolling(container, api, u, initialStatus) {
  const isActive = initialStatus.queue_processing || initialStatus.queue_pending > 0;
  const interval = isActive ? 3000 : 10000;

  _pollTimer = setInterval(async () => {
    if (!container.querySelector('#sm-processing-banner')) {
      _stopPolling();
      return;
    }
    try {
      const status = await api.get('/api/structured-memory/status');

      const banner = container.querySelector('#sm-processing-banner');
      const bannerText = container.querySelector('#sm-banner-text');
      if (banner) {
        if (status.queue_processing || status.queue_pending > 0) {
          banner.classList.remove('hidden');
          if (bannerText) bannerText.textContent = _getBannerText(status);
        } else {
          banner.classList.add('hidden');
        }
      }

      const queueEl = container.querySelector('#sm-stat-queue');
      if (queueEl) queueEl.innerHTML = _queueBadge(status);

      const factsEl = container.querySelector('#sm-stat-facts');
      if (factsEl) factsEl.textContent = status.facts_count;

      const updatedEl = container.querySelector('#sm-stat-updated');
      if (updatedEl && status.lastUpdated) updatedEl.textContent = u.timeAgo(status.lastUpdated);

      const filledSections = Object.values(status.sections || {}).filter(Boolean).length;
      const totalSections = Object.keys(status.sections || {}).length;
      const secEl = container.querySelector('#sm-stat-sections');
      if (secEl) secEl.textContent = `${filledSections}/${totalSections}`;

      if (status.lastUpdated !== _lastUpdated && !status.queue_processing) {
        _lastUpdated = status.lastUpdated;
        _stopPolling();
        await render(container);
      } else {
        const newInterval = (status.queue_processing || status.queue_pending > 0) ? 3000 : 10000;
        if (newInterval !== interval) {
          _stopPolling();
          _startPolling(container, api, u, status);
        }
      }
    } catch { /* network hiccup, keep polling */ }
  }, interval);
}

function _stopPolling() {
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
}

function renderSectionGroup(title, groupData, sections, u) {
  const sectionCards = sections.map(([key, label]) => {
    const entry = groupData[key];
    if (!entry || typeof entry !== 'object') {
      return `
        <div class="bg-surface-800/50 rounded-lg p-3 border border-surface-700/50">
          <div class="text-xs font-medium text-zinc-500 mb-1">${u.escapeHtml(label)}</div>
          <div class="text-xs text-zinc-600 italic">No data yet</div>
        </div>`;
    }
    const summary = entry.summary || '';
    const updatedAt = entry.updatedAt ? u.timeAgo(entry.updatedAt) : '';
    return `
      <div class="bg-surface-800/50 rounded-lg p-3 border border-surface-700/50">
        <div class="flex items-center justify-between mb-1">
          <div class="text-xs font-medium text-zinc-300">${u.escapeHtml(label)}</div>
          ${updatedAt ? `<span class="text-[10px] text-zinc-600">${u.escapeHtml(updatedAt)}</span>` : ''}
        </div>
        <div class="text-xs text-zinc-400 leading-relaxed">${summary ? u.escapeHtml(summary) : '<span class="text-zinc-600 italic">Empty</span>'}</div>
      </div>`;
  }).join('');

  return `
    <div class="mt-6">
      <h2 class="text-sm font-semibold text-zinc-400 mb-3">${u.escapeHtml(title)}</h2>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        ${sectionCards}
      </div>
    </div>`;
}

function renderFactsList(facts, u) {
  if (!facts.length) {
    return '<div class="text-xs text-zinc-600 text-center py-6">No facts stored yet. Chat with Quinely to build memory.</div>';
  }

  const validFacts = facts.filter(f => f && typeof f === 'object');
  const sorted = validFacts.sort((a, b) => {
    const ca = parseFloat(a.confidence) || 0;
    const cb = parseFloat(b.confidence) || 0;
    return cb - ca;
  });

  return sorted.map(f => {
    const conf = parseFloat(f.confidence) || 0;
    const pct = Math.round(conf * 100);
    const barColor = conf >= 0.9 ? 'bg-emerald-500' : conf >= 0.7 ? 'bg-blue-500' : conf >= 0.5 ? 'bg-amber-500' : 'bg-red-500';
    const catColors = {
      preference: 'bg-purple-500/15 text-purple-400',
      knowledge: 'bg-blue-500/15 text-blue-400',
      context: 'bg-emerald-500/15 text-emerald-400',
      behavior: 'bg-amber-500/15 text-amber-400',
      goal: 'bg-pink-500/15 text-pink-400',
    };
    const catClass = catColors[f.category] || 'bg-zinc-500/15 text-zinc-400';
    const created = f.createdAt ? u.timeAgo(f.createdAt) : '';

    return `
      <div class="fact-row flex items-center gap-3 bg-surface-800/40 rounded-lg px-3 py-2 border border-surface-700/30 hover:border-surface-600/50 transition-colors" data-fact-id="${u.escapeHtml(f.id || '')}">
        <div class="flex-shrink-0 w-12">
          <div class="w-full bg-surface-700 rounded-full h-1.5">
            <div class="${barColor} h-1.5 rounded-full" style="width:${pct}%"></div>
          </div>
          <div class="text-[9px] text-zinc-600 text-center mt-0.5">${pct}%</div>
        </div>
        <div class="flex-1 min-w-0">
          <div class="text-xs text-zinc-300 leading-relaxed">${u.escapeHtml(f.content || '')}</div>
        </div>
        <div class="flex items-center gap-2 flex-shrink-0">
          <span class="text-[10px] px-1.5 py-0.5 rounded ${catClass}">${u.escapeHtml(f.category || '?')}</span>
          ${created ? `<span class="text-[9px] text-zinc-600">${u.escapeHtml(created)}</span>` : ''}
          <button class="fact-delete-btn text-zinc-600 hover:text-red-400 transition-colors" data-fact-id="${u.escapeHtml(f.id || '')}" title="Delete fact">
            <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
          </button>
        </div>
      </div>`;
  }).join('');
}

function bindEvents(container, api, u, memData) {
  container.querySelector('#sm-refresh')?.addEventListener('click', async () => {
    const btn = container.querySelector('#sm-refresh');
    btn.disabled = true;
    btn.textContent = 'Refreshing...';
    try {
      await api.post('/api/structured-memory/refresh');
      u.toast('Memory refreshed', 'success');
      await render(container);
    } catch (e) {
      u.toast(e.message || 'Refresh failed', 'error');
      btn.disabled = false;
      btn.textContent = 'Refresh';
    }
  });

  container.querySelector('#sm-fact-filter')?.addEventListener('change', (e) => {
    const category = e.target.value;
    const facts = (memData.facts || []).filter(f => f && typeof f === 'object');
    const filtered = category ? facts.filter(f => f.category === category) : facts;
    const list = container.querySelector('#sm-facts-list');
    if (list) list.innerHTML = renderFactsList(filtered, u);
    bindDeleteButtons(container, api, u);
  });

  bindDeleteButtons(container, api, u);
}

function bindDeleteButtons(container, api, u) {
  container.querySelectorAll('.fact-delete-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const factId = btn.dataset.factId;
      if (!factId) return;
      if (!confirm('Delete this fact?')) return;
      btn.disabled = true;
      try {
        const result = await api.del(`/api/structured-memory/facts/${encodeURIComponent(factId)}`);
        if (result.status === 'ok') {
          const row = btn.closest('.fact-row');
          if (row) row.remove();
          u.toast('Fact deleted', 'success');
        } else {
          u.toast(result.error || 'Delete failed', 'error');
          btn.disabled = false;
        }
      } catch (e) {
        u.toast(e.message || 'Delete failed', 'error');
        btn.disabled = false;
      }
    });
  });
}
