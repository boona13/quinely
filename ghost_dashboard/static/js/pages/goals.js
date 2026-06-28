/** Goals page — persistent multi-step user goals with autonomous execution */

let currentGoals = [];
let currentFilter = 'all';

const DELIVERY_OPTIONS = [
  { value: '',          label: 'Dashboard only',   icon: '🖥️' },
  { value: 'notify',    label: 'Push notification', icon: '🔔' },
  { value: 'telegram',  label: 'Telegram',          icon: '✈️' },
  { value: 'discord',   label: 'Discord',           icon: '💬' },
  { value: 'chat',      label: 'Activity feed',     icon: '📋' },
  { value: 'memory',    label: 'Save to memory',    icon: '🧠' },
];

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;

  const [listData, statsData] = await Promise.all([
    api.get('/api/goals/list'),
    api.get('/api/goals/stats'),
  ]);

  currentGoals = listData.goals || [];
  const stats = statsData || {};

  container.innerHTML = `
    <div class="flex items-center justify-between mb-1">
      <h1 class="page-header">Goals</h1>
      <button id="goal-add-btn" class="btn btn-primary btn-sm flex items-center gap-1.5">
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
        New Goal
      </button>
    </div>
    <p class="page-desc">Persistent multi-step goals executed autonomously — recurring tasks, long-horizon projects, weekly reports.</p>

    <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
      ${statCard('Total', stats.total || 0, 'text-zinc-300')}
      ${statCard('Active', stats.active || 0, 'text-emerald-400')}
      ${statCard('Needs Plan', stats.pending_plan || 0, 'text-amber-400')}
      ${statCard('Paused', stats.paused || 0, 'text-zinc-500')}
      ${statCard('Completed', stats.completed || 0, 'text-ghost-400')}
    </div>

    <div class="border-b border-surface-600/30 mb-4">
      <nav class="flex gap-1">
        ${tab('all', 'All', currentGoals.length)}
        ${tab('active', 'Active', stats.active || 0)}
        ${tab('pending_plan', 'Needs Plan', stats.pending_plan || 0)}
        ${tab('paused', 'Paused', stats.paused || 0)}
        ${tab('completed', 'Completed', stats.completed || 0)}
        ${tab('abandoned', 'Abandoned', stats.abandoned || 0)}
      </nav>
    </div>

    <div id="goal-list" class="space-y-3">
      ${renderList(currentGoals, currentFilter)}
    </div>

    <!-- Add Goal Modal -->
    <div id="goal-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center" style="background:rgba(0,0,0,0.7)">
      <div class="stat-card w-full max-w-lg mx-4" style="border-color:rgba(139,92,246,0.3);max-height:90vh;overflow-y:auto">
        <h3 class="text-sm font-bold text-white mb-4">New Goal</h3>
        <form id="goal-form" class="space-y-3">
          <div>
            <label class="form-label">Title</label>
            <input type="text" id="goal-title" required placeholder="e.g. Weekly AI News Digest" class="form-input w-full">
          </div>
          <div>
            <label class="form-label">Goal Description</label>
            <textarea id="goal-text" rows="3" required placeholder="Describe what Ghost should do — be specific about steps and output." class="form-input w-full" style="resize:vertical"></textarea>
          </div>
          <div>
            <label class="form-label">Recurrence <span class="text-zinc-500 font-normal">(optional cron expression)</span></label>
            <input type="text" id="goal-recurrence" placeholder="e.g. 0 9 * * 1  (every Monday 9am)" class="form-input w-full font-mono text-xs">
            <div class="flex flex-wrap gap-1.5 mt-1.5" id="recurrence-presets">
              ${['0 9 * * 1|Every Mon 9am','0 9 * * *|Every day 9am','0 9 * * 1-5|Weekdays 9am','0 9 1 * *|Monthly'].map(p => {
                const [val, label] = p.split('|');
                return `<button type="button" class="recurrence-preset text-[10px] px-2 py-0.5 rounded-full border border-zinc-700 text-zinc-400 hover:border-ghost-500 hover:text-ghost-400 transition-colors" data-val="${val}">${label}</button>`;
              }).join('')}
            </div>
          </div>
          <div>
            <label class="form-label">Delivery <span class="text-zinc-500 font-normal">(how to receive results)</span></label>
            <div class="grid grid-cols-3 gap-1.5" id="delivery-picker">
              ${DELIVERY_OPTIONS.map(opt => `
                <button type="button" class="delivery-option flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-[11px] transition-all
                  ${opt.value === '' ? 'border-ghost-500/50 bg-ghost-500/10 text-ghost-400' : 'border-zinc-700 text-zinc-400 hover:border-ghost-500/40 hover:text-ghost-300'}"
                  data-delivery="${opt.value}">
                  <span>${opt.icon}</span>
                  <span>${opt.label}</span>
                </button>
              `).join('')}
            </div>
            <input type="hidden" id="goal-delivery" value="">
          </div>
          <div class="flex justify-end gap-2 pt-2">
            <button type="button" id="goal-cancel" class="btn btn-secondary btn-sm">Cancel</button>
            <button type="submit" class="btn btn-primary btn-sm">Create Goal</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Detail Drawer -->
    <div id="goal-drawer" class="hidden fixed inset-y-0 right-0 z-50 w-full max-w-lg flex flex-col" style="background:#18181b;border-left:1px solid rgba(63,63,70,0.5)">
      <div id="goal-drawer-content" class="flex-1 overflow-y-auto p-5"></div>
      <div class="p-4 border-t border-zinc-800">
        <button id="goal-drawer-close" class="btn btn-secondary btn-sm w-full">Close</button>
      </div>
    </div>
    <div id="goal-drawer-backdrop" class="hidden fixed inset-0 z-40" style="background:rgba(0,0,0,0.4)"></div>
  `;

  bindEvents(container, api, u);
}

// ─── render helpers ──────────────────────────────────────────────────────────

function statCard(label, value, colorClass) {
  return `
    <div class="stat-card">
      <div class="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">${label}</div>
      <div class="text-xl font-bold ${colorClass}">${value}</div>
    </div>`;
}

function tab(filter, label, count) {
  const active = filter === currentFilter ? 'border-b-2 border-ghost-500 text-white' : 'text-zinc-500 hover:text-zinc-300';
  return `<button class="goal-tab px-3 py-2 text-xs font-medium transition-colors ${active}" data-filter="${filter}">
    ${label} <span class="text-[10px] text-zinc-600">${count}</span>
  </button>`;
}

function statusBadge(status) {
  const map = {
    active:       ['bg-emerald-500/15 text-emerald-400', 'Active'],
    pending_plan: ['bg-amber-500/15 text-amber-400',    'Needs Plan'],
    paused:       ['bg-zinc-500/15 text-zinc-400',       'Paused'],
    completed:    ['bg-ghost-500/15 text-ghost-400',     'Completed'],
    abandoned:    ['bg-red-500/15 text-red-400',         'Abandoned'],
  };
  const [cls, lbl] = map[status] || ['bg-zinc-700 text-zinc-400', status];
  return `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium ${cls}">${lbl}</span>`;
}

function progressBar(plan) {
  if (!plan || plan.length === 0) return '';
  const done = plan.filter(s => s.status === 'completed').length;
  const failed = plan.filter(s => s.status === 'failed').length;
  const pct = Math.round((done / plan.length) * 100);
  return `
    <div class="mt-2">
      <div class="flex items-center justify-between text-[10px] text-zinc-500 mb-1">
        <span>${done}/${plan.length} steps${failed > 0 ? ` · <span class="text-red-400">${failed} failed</span>` : ''}</span><span>${pct}%</span>
      </div>
      <div class="h-1 bg-zinc-800 rounded-full overflow-hidden">
        <div class="h-full bg-ghost-500 rounded-full transition-all" style="width:${pct}%"></div>
      </div>
    </div>`;
}

function recurrenceLabel(recurrence) {
  if (!recurrence) return '';
  const presets = {
    '0 9 * * 1': 'Every Monday 9am',
    '0 9 * * *': 'Every day 9am',
    '0 8 * * *': 'Every day 8am',
    '0 9 * * 1-5': 'Weekdays 9am',
    '0 9 1 * *': '1st of month',
    '*/30 * * * *': 'Every 30 min',
    '0 */6 * * *': 'Every 6 hours',
  };
  const label = presets[recurrence] || recurrence;
  return `<span class="inline-flex items-center gap-1 text-[10px] text-zinc-500">
    <svg class="w-2.5 h-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
    ${label}
  </span>`;
}

function deliveryLabel(delivery) {
  if (!delivery) return '';
  const opt = DELIVERY_OPTIONS.find(o => o.value === delivery);
  const icon = opt ? opt.icon : '📤';
  const label = opt ? opt.label : delivery;
  return `<span class="inline-flex items-center gap-1 text-[10px] text-zinc-500">
    <span>${icon}</span> ${label}
  </span>`;
}

function completionBadge(count) {
  if (!count || count < 1) return '';
  return `<span class="inline-flex items-center gap-0.5 text-[10px] text-ghost-400 font-medium">
    <svg class="w-2.5 h-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
    ${count}×
  </span>`;
}

function renderList(goals, filter) {
  const filtered = filter === 'all' ? goals : goals.filter(g => g.status === filter);
  if (filtered.length === 0) {
    if (filter === 'all') {
      return `<div class="text-center py-16">
        <div class="text-3xl mb-3 opacity-80">\uD83C\uDFAF</div>
        <div class="text-sm text-zinc-300">No goals yet.</div>
        <div class="text-xs text-zinc-600 mt-1">Hand Ghost something to chase \u2014 it'll plan the steps and work them on its own.</div>
      </div>`;
    }
    return `<div class="text-center text-zinc-600 py-16 text-sm">Nothing ${filter} right now.</div>`;
  }
  return filtered.map(g => `
    <div class="stat-card cursor-pointer hover:border-ghost-500/40 transition-colors goal-card" data-id="${g.id}" role="button" tabindex="0" aria-label="View goal: ${escHtml(g.title)}" style="border-color:rgba(63,63,70,0.4)">
      <div class="flex items-start justify-between gap-3">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 mb-1 flex-wrap">
            ${statusBadge(g.status)}
            ${recurrenceLabel(g.recurrence)}
            ${deliveryLabel(g.delivery)}
            ${completionBadge(g.completion_count)}
          </div>
          <div class="text-sm font-semibold text-white truncate">${escHtml(g.title)}</div>
          <div class="text-xs text-zinc-500 mt-0.5 line-clamp-2">${escHtml(g.goal_text || '')}</div>
          ${progressBar(g.plan)}
        </div>
        <div class="flex items-center gap-1 shrink-0">
          ${g.status === 'active' || g.status === 'pending_plan' ? `<button class="goal-action-btn btn btn-ghost btn-sm py-0.5 px-2 text-[11px] text-ghost-400 hover:bg-ghost-500/10" data-action="run" data-id="${g.id}" title="Run now">▶</button>` : ''}
          ${g.status === 'active' ? `<button class="goal-action-btn btn btn-secondary btn-sm py-0.5 px-2 text-[11px]" data-action="pause" data-id="${g.id}">Pause</button>` : ''}
          ${g.status === 'paused' ? `<button class="goal-action-btn btn btn-primary btn-sm py-0.5 px-2 text-[11px]" data-action="resume" data-id="${g.id}">Resume</button>` : ''}
          <button class="goal-action-btn p-1.5 rounded hover:bg-zinc-700 text-zinc-500 hover:text-red-400 transition-colors" data-action="abandon" data-id="${g.id}" title="Abandon goal">
            <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
          </button>
        </div>
      </div>
      ${g.last_output ? `
        <div class="mt-2 pt-2 border-t border-emerald-500/10 text-[10px]">
          <span class="text-emerald-600">Latest output:</span>
          <span class="text-zinc-500"> ${escHtml(g.last_output.slice(0, 140))}${g.last_output.length > 140 ? '…' : ''}</span>
        </div>` :
        (g.observations || []).length > 0 ? `
        <div class="mt-2 pt-2 border-t border-zinc-800/60 text-[10px] text-zinc-500">
          <span class="text-zinc-600">Last note:</span> ${escHtml((g.observations[g.observations.length - 1]?.text || '').slice(0, 120))}
        </div>` : ''}
    </div>
  `).join('');
}

function renderStepRow(step, i) {
  const stepIcon = s => {
    if (s === 'completed') return `<svg class="w-4 h-4 text-emerald-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>`;
    if (s === 'failed')    return `<svg class="w-4 h-4 text-red-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>`;
    if (s === 'running')   return `<div class="w-4 h-4 border-2 border-ghost-400 border-t-transparent rounded-full animate-spin shrink-0"></div>`;
    return `<div class="w-4 h-4 rounded-full border-2 border-zinc-600 shrink-0"></div>`;
  };
  const retryBadge = (step.retry_count && step.retry_count > 0)
    ? `<span class="text-[9px] text-amber-500 ml-1">(retry ${step.retry_count})</span>` : '';
  return `
    <div class="flex gap-3 items-start p-2 rounded-lg ${step.status === 'completed' ? 'bg-emerald-500/5' : step.status === 'failed' ? 'bg-red-500/5' : 'bg-zinc-800/40'}">
      ${stepIcon(step.status)}
      <div class="flex-1 min-w-0">
        <div class="text-[10px] text-zinc-600 mb-0.5">Step ${i + 1}${retryBadge}</div>
        <div class="text-xs text-zinc-300 leading-snug">${escHtml(step.description)}</div>
        ${step.result ? `<div class="text-[10px] text-zinc-500 mt-0.5 italic">${escHtml(step.result.slice(0, 150))}</div>` : ''}
        ${step.error  ? `<div class="text-[10px] text-red-400 mt-0.5">${escHtml(step.error.slice(0, 150))}</div>` : ''}
      </div>
    </div>`;
}

function renderPlanSection(goal) {
  const plan = goal.plan || [];
  const lastRun = goal.last_completed_plan || null;
  const isRecurring = !!goal.recurrence;
  const allPending = plan.length > 0 && plan.every(s => s.status === 'pending');

  if (isRecurring && allPending && lastRun) {
    const lastSteps = lastRun.steps || [];
    const doneCount = lastSteps.filter(s => s.status === 'completed').length;
    const lastDate = (lastRun.completed_at || '').slice(0, 16).replace('T', ' ');
    return `
      <div class="mb-5">
        <div class="flex items-center justify-between mb-2">
          <div class="text-[10px] uppercase tracking-wider text-emerald-600 font-semibold">Last Run — All Steps Completed</div>
          <span class="text-[10px] text-zinc-500">${lastDate} · Run #${lastRun.run || ''}</span>
        </div>
        <div class="space-y-2">
          ${lastSteps.map((step, i) => renderStepRow(step, i)).join('')}
        </div>
      </div>
      <div class="mb-2">
        <div class="text-[10px] uppercase tracking-wider text-zinc-600 mb-2">Next Cycle — Waiting to Run</div>
        <div class="space-y-2 mb-5">
          ${plan.map((step, i) => renderStepRow(step, i)).join('')}
        </div>
      </div>`;
  }

  if (plan.length === 0) {
    return `<div class="text-[10px] uppercase tracking-wider text-zinc-600 mb-2">Execution Plan</div>
      <div class="text-xs text-zinc-600 italic mb-4">No plan yet — the goal executor will create one on its next run.</div>`;
  }
  return `
    <div class="text-[10px] uppercase tracking-wider text-zinc-600 mb-2">Execution Plan</div>
    <div class="space-y-2 mb-5">
      ${plan.map((step, i) => renderStepRow(step, i)).join('')}
    </div>`;
}

function renderDrawer(goal) {
  const observations = goal.observations || [];
  const outputHistory = goal.output_history || [];
  const lastOutput = goal.last_output || null;
  const delivery = goal.delivery || '';
  const scratch = goal.scratch || {};
  const scratchKeys = Object.keys(scratch);
  const canRun = goal.status === 'active' || goal.status === 'pending_plan';

  return `
    <div class="flex items-start justify-between mb-4">
      <div class="flex-1">
        <div class="flex items-center gap-2 mb-1 flex-wrap">${statusBadge(goal.status)} ${recurrenceLabel(goal.recurrence)} ${completionBadge(goal.completion_count)}</div>
        <h2 class="text-base font-bold text-white">${escHtml(goal.title)}</h2>
        <p class="text-xs text-zinc-500 mt-1">${escHtml(goal.goal_text || '')}</p>
      </div>
    </div>

    <!-- Action bar -->
    <div class="flex items-center gap-2 mb-4 flex-wrap">
      ${canRun ? `<button id="drawer-run-btn" class="btn btn-primary btn-sm flex items-center gap-1.5 text-[11px]" data-id="${goal.id}">
        <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
        Run Now
      </button>` : ''}
      <button id="drawer-delete-btn" class="btn btn-sm flex items-center gap-1.5 text-[11px] text-red-400 hover:bg-red-500/10 border border-red-500/20 hover:border-red-500/40 transition-colors" data-id="${goal.id}">
        <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
        Delete
      </button>
    </div>

    <!-- Delivery selector -->
    <div class="mb-5">
      <div class="text-[10px] uppercase tracking-wider text-zinc-600 mb-2">Delivery Method</div>
      <div class="flex flex-wrap gap-1.5" id="drawer-delivery-picker" data-goal-id="${goal.id}">
        ${DELIVERY_OPTIONS.map(opt => `
          <button class="drawer-delivery-btn flex items-center gap-1 px-2 py-1 rounded-lg border text-[10px] transition-all
            ${opt.value === delivery ? 'border-ghost-500/50 bg-ghost-500/10 text-ghost-400' : 'border-zinc-700/60 text-zinc-500 hover:border-ghost-500/30 hover:text-zinc-300'}"
            data-delivery="${opt.value}">
            <span>${opt.icon}</span> ${opt.label}
          </button>
        `).join('')}
      </div>
    </div>

    ${lastOutput ? `
      <div class="mb-5">
        <div class="flex items-center justify-between mb-2">
          <div class="text-[10px] uppercase tracking-wider text-emerald-500 font-semibold">Latest Output</div>
          ${outputHistory.length > 1 ? `<button id="toggle-history" class="text-[10px] text-zinc-500 hover:text-zinc-300 transition-colors">${outputHistory.length - 1} previous run${outputHistory.length > 2 ? 's' : ''} ▾</button>` : ''}
        </div>
        <div class="p-3 rounded-lg bg-emerald-500/5 border border-emerald-500/20">
          <div class="text-[10px] text-zinc-500 mb-2">
            ${goal.last_executed_at ? goal.last_executed_at.slice(0, 16).replace('T', ' ') : ''}
            ${goal.completion_count > 0 ? `· Run #${goal.completion_count}` : ''}
          </div>
          <div class="text-xs text-zinc-300 leading-relaxed whitespace-pre-wrap" style="max-height:320px;overflow-y:auto">${escHtml(lastOutput)}</div>
        </div>
        ${outputHistory.length > 1 ? `
          <div id="output-history" class="hidden mt-2 space-y-2">
            ${outputHistory.slice(0, -1).reverse().map((h, i) => `
              <div class="p-3 rounded-lg bg-zinc-800/40 border border-zinc-700/40">
                <div class="text-[10px] text-zinc-500 mb-1.5">${h.at ? h.at.slice(0, 16).replace('T', ' ') : ''} · Run #${h.run || (outputHistory.length - 1 - i)}</div>
                <div class="text-xs text-zinc-400 leading-relaxed whitespace-pre-wrap" style="max-height:200px;overflow-y:auto">${escHtml(h.output || '')}</div>
              </div>
            `).join('')}
          </div>
        ` : ''}
      </div>
    ` : `
      <div class="mb-5 p-3 rounded-lg bg-zinc-800/30 border border-zinc-700/30 text-xs text-zinc-500 italic">
        No output yet — Ghost will populate this after completing the first execution cycle.
      </div>
    `}

    ${renderPlanSection(goal)}

    ${scratchKeys.length > 0 ? `
      <div class="text-[10px] uppercase tracking-wider text-zinc-600 mb-2">Scratch Space <span class="text-zinc-700">(inter-step data)</span></div>
      <div class="space-y-1.5 mb-5">
        ${scratchKeys.map(k => {
          const val = typeof scratch[k] === 'string' ? scratch[k] : JSON.stringify(scratch[k]);
          return `
            <div class="p-2 rounded bg-zinc-800/40 border-l-2 border-amber-500/20">
              <div class="text-[10px] text-amber-500/80 font-mono mb-0.5">${escHtml(k)}</div>
              <div class="text-[10px] text-zinc-500 leading-snug truncate" style="max-width:100%">${escHtml(val.slice(0, 200))}</div>
            </div>`;
        }).join('')}
      </div>
    ` : ''}

    ${observations.length > 0 ? `
      <div class="text-[10px] uppercase tracking-wider text-zinc-600 mb-2">Working Memory</div>
      <div class="space-y-2 mb-5">
        ${observations.slice().reverse().slice(0, 8).map(o => `
          <div class="p-2 rounded bg-zinc-800/40 border-l-2 border-ghost-500/20">
            <div class="text-[10px] text-zinc-600 mb-0.5">${o.at ? o.at.slice(0, 16).replace('T', ' ') : ''}</div>
            <div class="text-xs text-zinc-400 leading-snug">${escHtml(o.text || '')}</div>
          </div>
        `).join('')}
      </div>
    ` : ''}

    <!-- Self-Improvement: loaded async -->
    <div id="goal-improvements-section" class="hidden mb-5">
      <div class="flex items-center gap-2 mb-2">
        <div class="text-[10px] uppercase tracking-wider text-violet-400 font-semibold">Self-Improvement</div>
        <span class="text-[9px] px-1.5 py-0.5 rounded-full bg-violet-500/10 border border-violet-500/20 text-violet-400" id="improvements-count"></span>
      </div>
      <p class="text-[10px] text-zinc-600 mb-2">Ghost analyzed this goal's execution and submitted these improvements to its own codebase:</p>
      <div id="goal-improvements-list" class="space-y-1.5"></div>
    </div>

    <div class="text-[10px] text-zinc-600 mt-4 pt-4 border-t border-zinc-800">
      ID: <span class="font-mono">${goal.id}</span>
      &nbsp;·&nbsp; Created: ${(goal.created_at || '').slice(0, 16).replace('T', ' ')}
      ${goal.last_executed_at ? `&nbsp;·&nbsp; Last run: ${goal.last_executed_at.slice(0, 16).replace('T', ' ')}` : ''}
      ${goal.completion_count > 0 ? `&nbsp;·&nbsp; Completed ${goal.completion_count}× ` : ''}
    </div>
  `;
}

// ─── event binding ────────────────────────────────────────────────────────────

function bindEvents(container, api, u) {
  // New goal modal
  document.getElementById('goal-add-btn').addEventListener('click', () => {
    document.getElementById('goal-modal').classList.remove('hidden');
    document.getElementById('goal-title').focus();
  });

  document.getElementById('goal-cancel').addEventListener('click', closeModal);
  document.getElementById('goal-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });

  // Recurrence presets
  document.getElementById('recurrence-presets').addEventListener('click', e => {
    const btn = e.target.closest('.recurrence-preset');
    if (btn) document.getElementById('goal-recurrence').value = btn.dataset.val;
  });

  // Delivery picker in create modal
  document.getElementById('delivery-picker').addEventListener('click', e => {
    const btn = e.target.closest('.delivery-option');
    if (!btn) return;
    const val = btn.dataset.delivery;
    document.getElementById('goal-delivery').value = val;
    document.querySelectorAll('.delivery-option').forEach(b => {
      const isActive = b.dataset.delivery === val;
      b.classList.toggle('border-ghost-500/50', isActive);
      b.classList.toggle('bg-ghost-500/10', isActive);
      b.classList.toggle('text-ghost-400', isActive);
      b.classList.toggle('border-zinc-700', !isActive);
      b.classList.toggle('text-zinc-400', !isActive);
    });
  });

  // Create goal form
  document.getElementById('goal-form').addEventListener('submit', async e => {
    e.preventDefault();
    const data = {
      title:      document.getElementById('goal-title').value.trim(),
      goal_text:  document.getElementById('goal-text').value.trim(),
      recurrence: document.getElementById('goal-recurrence').value.trim(),
      delivery:   document.getElementById('goal-delivery').value,
    };
    const btn = e.target.querySelector('[type=submit]');
    btn.disabled = true; btn.textContent = 'Creating…';
    const res = await api.post('/api/goals/add', data);
    btn.disabled = false; btn.textContent = 'Create Goal';
    if (res.ok) {
      closeModal();
      u.toast('Goal created — Ghost will plan and execute it automatically.');
      await refresh(api);
    } else {
      u.toast(res.error || 'Failed to create goal', 'error');
    }
  });

  // Filter tabs
  container.querySelectorAll('.goal-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      currentFilter = tab.dataset.filter;
      document.getElementById('goal-list').innerHTML = renderList(currentGoals, currentFilter);
      rebindCards(api, u);
      container.querySelectorAll('.goal-tab').forEach(t => {
        t.classList.toggle('border-b-2', t.dataset.filter === currentFilter);
        t.classList.toggle('border-ghost-500', t.dataset.filter === currentFilter);
        t.classList.toggle('text-white', t.dataset.filter === currentFilter);
        t.classList.toggle('text-zinc-500', t.dataset.filter !== currentFilter);
      });
    });
  });

  rebindCards(api, u);

  // Drawer close
  document.getElementById('goal-drawer-close').addEventListener('click', closeDrawer);
  document.getElementById('goal-drawer-backdrop').addEventListener('click', closeDrawer);
}

function rebindCards(api, u) {
  document.querySelectorAll('.goal-card').forEach(card => {
    const openCard = async (e) => {
      if (e.target.closest('.goal-action-btn')) return;
      const id = card.dataset.id;
      const res = await api.get(`/api/goals/${id}`);
      if (res.ok) openDrawer(res.goal, api, u);
    };
    card.addEventListener('click', openCard);
    card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openCard(e); } });
  });

  document.querySelectorAll('.goal-action-btn').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      const { action, id } = btn.dataset;
      if (action === 'abandon' && !confirm('Abandon this goal? This cannot be undone.')) return;
      if (action === 'run') {
        btn.disabled = true; btn.textContent = '…';
        const res = await api.post(`/api/goals/${id}/run`);
        btn.disabled = false; btn.textContent = '▶';
        if (res.ok) {
          u.toast('Goal execution started — check back in a few minutes.');
        } else {
          u.toast(res.error || 'Failed to start', 'error');
        }
        return;
      }
      const res = await api.post(`/api/goals/${id}/${action}`);
      if (res.ok) {
        u.toast(action === 'pause' ? 'Goal paused.' : action === 'resume' ? 'Goal resumed.' : 'Goal abandoned.');
        await refresh(api);
      } else {
        u.toast(res.error || 'Action failed', 'error');
      }
    });
  });
}

async function refresh(api) {
  const [listData, statsData] = await Promise.all([
    api.get('/api/goals/list'),
    api.get('/api/goals/stats'),
  ]);
  currentGoals = listData.goals || [];
  const stats = statsData || {};

  const statValues = [
    stats.total || 0, stats.active || 0, stats.pending_plan || 0,
    stats.paused || 0, stats.completed || 0,
  ];
  document.querySelectorAll('.stat-card .text-xl').forEach((el, i) => {
    if (i < statValues.length) el.textContent = statValues[i];
  });

  const tabCounts = {
    all: currentGoals.length,
    active: stats.active || 0,
    pending_plan: stats.pending_plan || 0,
    paused: stats.paused || 0,
    completed: stats.completed || 0,
    abandoned: stats.abandoned || 0,
  };
  document.querySelectorAll('.goal-tab').forEach(tab => {
    const filter = tab.dataset.filter;
    const countEl = tab.querySelector('span');
    if (countEl && tabCounts[filter] !== undefined) {
      countEl.textContent = tabCounts[filter];
    }
  });

  document.getElementById('goal-list').innerHTML = renderList(currentGoals, currentFilter);
  const { GhostAPI, GhostUtils } = window;
  rebindCards(GhostAPI, GhostUtils);
}

function openDrawer(goal, api, u) {
  document.getElementById('goal-drawer-content').innerHTML = renderDrawer(goal);
  document.getElementById('goal-drawer').classList.remove('hidden');
  document.getElementById('goal-drawer-backdrop').classList.remove('hidden');

  // Wire output history toggle
  const toggleBtn = document.getElementById('toggle-history');
  const historyEl = document.getElementById('output-history');
  if (toggleBtn && historyEl) {
    toggleBtn.addEventListener('click', () => {
      const hidden = historyEl.classList.toggle('hidden');
      toggleBtn.textContent = hidden
        ? `${(goal.output_history || []).length - 1} previous run${(goal.output_history || []).length > 2 ? 's' : ''} ▾`
        : 'Hide history ▴';
    });
  }

  // Wire Run Now button
  const runBtn = document.getElementById('drawer-run-btn');
  if (runBtn && api) {
    runBtn.addEventListener('click', async () => {
      runBtn.disabled = true; runBtn.textContent = 'Starting…';
      const res = await api.post(`/api/goals/${goal.id}/run`);
      runBtn.disabled = false; runBtn.innerHTML = `<svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg> Run Now`;
      if (res.ok) {
        u.toast('Goal execution started — check back in a few minutes.');
      } else {
        u.toast(res.error || 'Failed to start', 'error');
      }
    });
  }

  // Wire Delete button
  const deleteBtn = document.getElementById('drawer-delete-btn');
  if (deleteBtn && api) {
    deleteBtn.addEventListener('click', async () => {
      if (!confirm(`Permanently delete "${goal.title}"? This cannot be undone.`)) return;
      const res = await api.post(`/api/goals/${goal.id}/delete`);
      if (res.ok) {
        closeDrawer();
        u.toast('Goal deleted.');
        await refresh(api);
      } else {
        u.toast(res.error || 'Delete failed', 'error');
      }
    });
  }

  // Wire drawer delivery picker
  const deliveryPicker = document.getElementById('drawer-delivery-picker');
  if (deliveryPicker && api) {
    deliveryPicker.querySelectorAll('.drawer-delivery-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const val = btn.dataset.delivery;
        const res = await api.post(`/api/goals/${goal.id}/delivery`, { delivery: val });
        if (res.ok) {
          deliveryPicker.querySelectorAll('.drawer-delivery-btn').forEach(b => {
            const isActive = b.dataset.delivery === val;
            b.classList.toggle('border-ghost-500/50', isActive);
            b.classList.toggle('bg-ghost-500/10', isActive);
            b.classList.toggle('text-ghost-400', isActive);
            b.classList.toggle('border-zinc-700/60', !isActive);
            b.classList.toggle('text-zinc-500', !isActive);
          });
          u.toast(`Delivery set to: ${val || 'dashboard only'}`);
        } else {
          u.toast('Failed to update delivery', 'error');
        }
      });
    });
  }

  // Fetch self-improvements Ghost made from this goal
  if (api) {
    api.get(`/api/goals/${goal.id}/improvements`).then(data => {
      const section = document.getElementById('goal-improvements-section');
      const list = document.getElementById('goal-improvements-list');
      const count = document.getElementById('improvements-count');
      if (!section || !list || !data.improvements || data.improvements.length === 0) return;

      count.textContent = `${data.improvements.length} queued`;
      list.innerHTML = data.improvements.map(imp => {
        const priorityColor = imp.priority === 'P1' ? 'text-red-400 border-red-500/30 bg-red-500/5'
          : imp.priority === 'P2' ? 'text-amber-400 border-amber-500/30 bg-amber-500/5'
          : 'text-zinc-400 border-zinc-600/30 bg-zinc-700/20';
        const statusIcon = imp.status === 'implemented' || imp.status === 'completed'
          ? '<svg class="w-3 h-3 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>'
          : imp.status === 'in_progress'
          ? '<svg class="w-3 h-3 text-blue-400 animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>'
          : '<svg class="w-3 h-3 text-violet-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6"/></svg>';
        return `
          <div class="p-2.5 rounded-lg border ${priorityColor} transition-all hover:brightness-110">
            <div class="flex items-center gap-2 mb-1">
              ${statusIcon}
              <span class="text-[11px] font-semibold">${escHtml(imp.title)}</span>
              <span class="text-[9px] px-1 py-0.5 rounded font-mono">${imp.priority}</span>
              <span class="text-[9px] px-1 py-0.5 rounded bg-zinc-700/30 text-zinc-500">${imp.category}</span>
            </div>
            <p class="text-[10px] text-zinc-500 leading-snug ml-5">${escHtml((imp.description || '').slice(0, 200))}</p>
          </div>`;
      }).join('');
      section.classList.remove('hidden');
    }).catch(() => {});
  }
}

function closeDrawer() {
  document.getElementById('goal-drawer').classList.add('hidden');
  document.getElementById('goal-drawer-backdrop').classList.add('hidden');
}

function closeModal() {
  document.getElementById('goal-modal').classList.add('hidden');
  document.getElementById('goal-form').reset();
  document.getElementById('goal-delivery').value = '';
  document.querySelectorAll('.delivery-option').forEach((b, i) => {
    b.classList.toggle('border-ghost-500/50', i === 0);
    b.classList.toggle('bg-ghost-500/10', i === 0);
    b.classList.toggle('text-ghost-400', i === 0);
    b.classList.toggle('border-zinc-700', i !== 0);
    b.classList.toggle('text-zinc-400', i !== 0);
  });
}

function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
