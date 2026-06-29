/** Quinely Dashboard — Evolution Page */

const api = window.GhostAPI;
const u = window.GhostUtils;
const t = (key, params) => window.GhostI18n?.t(key, params) ?? key;

let _currentTab = 'history';
let _expandedDiff = null;

export async function render(container) {
  const [statsData, historyData, pendingData] = await Promise.all([
    api.get('/api/evolve/stats'),
    api.get('/api/evolve/history'),
    api.get('/api/evolve/pending'),
  ]);

  const stats = statsData;
  const history = (historyData.history || []).slice().reverse();
  const pending = pendingData.pending || [];

  container.innerHTML = `
    <div class="space-y-6">
      <div class="flex items-center justify-between">
        <div>
          <h1 class="text-2xl font-bold text-white">${t('evolve.title')}</h1>
          <p class="text-sm text-zinc-500 mt-1">${t('evolve.subtitle')}</p>
        </div>
        <div class="flex items-center gap-2">
          <span class="px-2 py-1 rounded text-[10px] font-bold uppercase tracking-wider"
                style="background:rgba(139,92,246,0.15);color:#a78bfa;border:1px solid rgba(139,92,246,0.3)">
            ${stats.total_evolutions} ${t('evolve.evolutions')}
          </span>
        </div>
      </div>

      <!-- Stats cards -->
      <div class="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div class="stat-card text-center">
          <div class="text-xs text-zinc-500">${t('common.total')}</div>
          <div class="text-lg font-bold text-white">${stats.total_evolutions}</div>
        </div>
        <div class="stat-card text-center">
          <div class="text-xs text-zinc-500">${t('evolve.deployed')}</div>
          <div class="text-lg font-bold text-emerald-400">${stats.deployed}</div>
        </div>
        <div class="stat-card text-center">
          <div class="text-xs text-zinc-500">${t('evolve.rolledBack')}</div>
          <div class="text-lg font-bold text-amber-400">${stats.rolled_back}</div>
        </div>
        <div class="stat-card text-center">
          <div class="text-xs text-zinc-500">${t('common.pending')}</div>
          <div class="text-lg font-bold ${pending.length > 0 ? 'text-rose-400' : 'text-zinc-400'}">${stats.pending_approvals}</div>
        </div>
        <div class="stat-card text-center">
          <div class="text-xs text-zinc-500">${t('evolve.backups')}</div>
          <div class="text-lg font-bold text-blue-400">${stats.backups}</div>
        </div>
      </div>

      <!-- Tabs -->
      <div class="flex gap-1 border-b border-surface-600/50 pb-0">
        <button data-tab="history" class="evo-tab ${_currentTab === 'history' ? 'active' : ''}">
          ${t('evolve.history')}
          <span class="ml-1 text-[10px] opacity-60">${history.length}</span>
        </button>
        <button data-tab="pending" class="evo-tab ${_currentTab === 'pending' ? 'active' : ''}">
          ${t('evolve.pendingApprovals')}
          ${pending.length > 0 ? '<span class="ml-1 w-4 h-4 rounded-full bg-rose-500 text-white text-[10px] inline-flex items-center justify-center">' + pending.length + '</span>' : ''}
        </button>
      </div>

      <!-- Tab content -->
      <div id="evo-tab-content"></div>
    </div>
  `;

  const tabContent = container.querySelector('#evo-tab-content');

  function renderTab(tab) {
    _currentTab = tab;
    container.querySelectorAll('.evo-tab').forEach(el => {
      el.classList.toggle('active', el.dataset.tab === tab);
    });

    if (tab === 'history') {
      renderHistory(tabContent, history);
    } else {
      renderPending(tabContent, pending, container);
    }
  }

  container.querySelectorAll('.evo-tab').forEach(el => {
    el.addEventListener('click', () => renderTab(el.dataset.tab));
  });

  renderTab(_currentTab);
}


function renderHistory(el, history) {
  if (!history.length) {
    el.innerHTML = `
      <div class="text-center py-16">
        <div class="text-4xl mb-3 opacity-30">🧬</div>
        <div class="text-zinc-500">${t('evolve.noEvolutions')}</div>
        <div class="text-xs text-zinc-600 mt-1">${t('evolve.noEvoDesc')}</div>
      </div>
    `;
    return;
  }

  el.innerHTML = `
    <div class="space-y-3">
      ${history.map(evo => renderEvolutionCard(evo)).join('')}
    </div>
  `;

  el.querySelectorAll('[data-action="view-diff"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const evoId = btn.dataset.evoId;
      if (_expandedDiff === evoId) {
        _expandedDiff = null;
        const diffEl = btn.closest('.evo-card').querySelector('.diff-container');
        if (diffEl) diffEl.remove();
        return;
      }
      _expandedDiff = evoId;
      await showDiff(btn, evoId);
    });
  });

  el.querySelectorAll('[data-action="rollback"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const evoId = btn.dataset.evoId;
      if (!confirm(t('evolve.rollbackConfirm', { id: evoId }))) return;
      const res = await api.post('/api/evolve/rollback/' + evoId);
      u.toast(res.ok ? t('evolve.rollbackInitiated') : (t('evolve.rollbackFailed') + ' ' + res.message), res.ok ? 'success' : 'error');
    });
  });
}


function renderEvolutionCard(evo) {
  const statusConfig = {
    deployed: { color: 'emerald', icon: '✓', label: t('evolve.deployed') },
    rolled_back: { color: 'amber', icon: '↩', label: t('evolve.rolledBack') },
    tested_pass: { color: 'blue', icon: '✓', label: t('evolve.testsPassed') },
    tested_fail: { color: 'red', icon: '✗', label: t('evolve.testsFailed') },
    planned: { color: 'purple', icon: '◎', label: t('evolve.planned') },
    pending_approval: { color: 'amber', icon: '⏳', label: t('evolve.awaitingApproval') },
    approved: { color: 'blue', icon: '✓', label: t('evolve.approvedStatus') },
  };

  const sc = statusConfig[evo.status] || { color: 'zinc', icon: '?', label: evo.status };
  const time = evo.created_at ? u.formatTime(evo.created_at) : '—';
  const changes = evo.changes || [];
  const files = changes.map(c => c.file).filter(Boolean);
  const fileList = files.length ? files.join(', ') : (evo.files || []).join(', ') || t('evolve.noFiles');

  return `
    <div class="evo-card rounded-lg p-4" style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06)">
      <div class="flex items-start justify-between gap-3">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 mb-1">
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider"
                  style="background:rgba(var(--${sc.color}),0.12);color:var(--${sc.color}-text);border:1px solid rgba(var(--${sc.color}),0.25)">
              <span class="status-badge-${sc.color}">${sc.icon} ${sc.label}</span>
            </span>
            <span class="text-[10px] font-mono text-zinc-600">${evo.id}</span>
          </div>
          <div class="text-sm text-white font-medium">${u.escapeHtml(evo.description || t('common.noDescription'))}</div>
          <div class="flex items-center gap-3 mt-1.5 text-[11px] text-zinc-500">
            <span>${time}</span>
            <span class="text-zinc-700">|</span>
            <span title="${u.escapeHtml(fileList)}">${t('evolve.fileCount', {n: files.length || (evo.files || []).length})}</span>
            ${evo.level ? '<span class="text-zinc-700">|</span><span>' + t('evolve.level', {n: evo.level}) + '</span>' : ''}
          </div>
        </div>
        <div class="flex items-center gap-1.5 flex-shrink-0">
          ${changes.length > 0 ? '<button data-action="view-diff" data-evo-id="' + evo.id + '" class="btn-icon" title="View diff"><svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"/></svg></button>' : ''}
          ${evo.status === 'deployed' ? '<button data-action="rollback" data-evo-id="' + evo.id + '" class="btn-icon text-amber-400" title="Rollback"><svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6"/></svg></button>' : ''}
        </div>
      </div>
    </div>
  `;
}


async function showDiff(btn, evoId) {
  const card = btn.closest('.evo-card');
  const existing = card.querySelector('.diff-container');
  if (existing) { existing.remove(); return; }

  const data = await api.get('/api/evolve/diff/' + evoId);
  const changes = data.changes || [];

  const diffHtml = changes.map(change => {
    const lines = (change.diff || t('evolve.noDiff')).split('\n');
    const rendered = lines.map(line => {
      let cls = 'text-zinc-500';
      if (line.startsWith('+') && !line.startsWith('+++')) cls = 'text-emerald-400';
      else if (line.startsWith('-') && !line.startsWith('---')) cls = 'text-rose-400';
      else if (line.startsWith('@@')) cls = 'text-blue-400';
      return '<div class="' + cls + '">' + u.escapeHtml(line) + '</div>';
    }).join('');

    return `
      <div class="mb-3">
        <div class="text-xs font-mono text-ghost-400 mb-1">${u.escapeHtml(change.file)}</div>
        <div class="font-mono text-[11px] leading-relaxed bg-surface-950 rounded p-3 overflow-x-auto max-h-64 overflow-y-auto">
          ${rendered}
        </div>
      </div>
    `;
  }).join('');

  const diffContainer = document.createElement('div');
  diffContainer.className = 'diff-container mt-3 pt-3 border-t border-surface-600/30';
  diffContainer.innerHTML = diffHtml || '<div class="text-zinc-600 text-sm">' + t('evolve.noDiff') + '</div>';
  card.appendChild(diffContainer);
}


function renderPending(el, pending, container) {
  if (!pending.length) {
    el.innerHTML = `
      <div class="text-center py-16">
        <div class="text-4xl mb-3 opacity-30">✓</div>
        <div class="text-zinc-500">${t('evolve.noPendingApprovals')}</div>
        <div class="text-xs text-zinc-600 mt-1">${t('evolve.pendingApprovalsDesc')}</div>
      </div>
    `;
    return;
  }

  el.innerHTML = `
    <div class="space-y-3">
      ${pending.map(evo => `
        <div class="rounded-lg p-4" style="background:rgba(245,158,11,0.05);border:1px solid rgba(245,158,11,0.2)">
          <div class="flex items-start justify-between gap-3">
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 mb-1">
                <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-amber-500/20 text-amber-400 border border-amber-500/30">
                  ${t('evolve.awaitingApproval')}
                </span>
                <span class="text-[10px] font-mono text-zinc-600">${evo.id}</span>
                ${evo.level ? '<span class="text-[10px] text-zinc-600">' + t('evolve.level', {n: evo.level}) + '</span>' : ''}
              </div>
              <div class="text-sm text-white font-medium">${u.escapeHtml(evo.description || t('common.noDescription'))}</div>
              <div class="text-[11px] text-zinc-500 mt-1">
                ${t('evolve.files')} ${(evo.files || []).map(f => '<code class="text-zinc-400">' + u.escapeHtml(f) + '</code>').join(', ') || t('evolve.noneListed')}
              </div>
              <div class="text-[11px] text-zinc-600 mt-0.5">${evo.created_at ? u.formatTime(evo.created_at) : ''}</div>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              <button data-action="approve" data-evo-id="${evo.id}" class="px-3 py-1.5 rounded text-xs font-medium bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/30 transition-colors">
                ${t('common.approve')}
              </button>
              <button data-action="reject" data-evo-id="${evo.id}" class="px-3 py-1.5 rounded text-xs font-medium bg-rose-500/20 text-rose-400 border border-rose-500/30 hover:bg-rose-500/30 transition-colors">
                ${t('common.reject')}
              </button>
            </div>
          </div>
        </div>
      `).join('')}
    </div>
  `;

  el.querySelectorAll('[data-action="approve"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const evoId = btn.dataset.evoId;
      const res = await api.post('/api/evolve/approve/' + evoId);
      u.toast(res.ok ? t('evolve.evoApproved') : t('common.failedWithError', {error: res.message}), res.ok ? 'success' : 'error');
      if (res.ok) render(container);
    });
  });

  el.querySelectorAll('[data-action="reject"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const evoId = btn.dataset.evoId;
      if (!confirm(t('evolve.rejectConfirm', { id: evoId }))) return;
      const res = await api.post('/api/evolve/reject/' + evoId);
      u.toast(res.ok ? t('evolve.evoRejected') : t('common.failedWithError', {error: res.message}), res.ok ? 'success' : 'error');
      if (res.ok) render(container);
    });
  });
}
