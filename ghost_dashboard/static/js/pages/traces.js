/** Run Traces page — per-invocation timeline: trigger → model/tool spans → outcome. */

let _timer = null;

function fmtAgo(epoch) {
  if (!epoch) return '—';
  const s = Math.floor(Date.now() / 1000 - epoch);
  if (s < 0) return 'now';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function fmtDur(ms) {
  if (!ms) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

function fmtTokens(n) {
  if (!n) return '0';
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

const SRC_COLOR = {
  chat: 'blue', cron: 'amber', channel: 'purple',
  monitor: 'cyan', action: 'emerald',
};
const STATUS_COLOR = {
  running: 'blue', ok: 'emerald', error: 'red', cancelled: 'zinc',
};

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;
  const esc = (s) => u.escapeHtml(String(s ?? ''));

  if (_timer) { clearInterval(_timer); _timer = null; }

  const badge = (label, color) =>
    `<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-${color}-500/20 text-${color}-400 font-medium uppercase tracking-wide">${esc(label)}</span>`;

  async function showDetail(runId) {
    if (_timer) { clearInterval(_timer); _timer = null; }
    let run;
    try {
      run = await api.get(`/api/traces/${encodeURIComponent(runId)}`);
    } catch (e) {
      container.innerHTML = `<div class="text-red-400 p-4">Failed to load run: ${esc(e)}</div>`;
      return;
    }
    if (run.error) {
      container.innerHTML = `<div class="text-zinc-500 p-4">Run not found (it may have rotated out of history).</div>`;
      return;
    }

    const spans = run.spans || [];
    const maxDur = Math.max(1, ...spans.map(s => s.duration_ms || 0));

    const spanRow = (s) => {
      const isModel = s.kind === 'model';
      const color = !s.ok ? 'red' : (isModel ? 'violet' : 'sky');
      const icon = isModel ? '◆' : '▸';
      const pct = Math.max(2, Math.round((s.duration_ms || 0) / maxDur * 100));
      const detail = isModel
        ? `${fmtTokens(s.total_tokens)} tok <span class="text-zinc-600">(${s.prompt_tokens || 0}→${s.completion_tokens || 0})</span>`
        : `<span class="text-zinc-400">${esc(s.result_preview || '').slice(0, 160)}</span>`;
      const args = (!isModel && s.args_summary && s.args_summary !== '{}')
        ? `<div class="text-[10px] text-zinc-500 font-mono mt-1 break-all">${esc(s.args_summary)}</div>` : '';
      const err = s.error ? `<div class="text-[10px] text-red-400 mt-1 break-all">${esc(s.error)}</div>` : '';
      return `<div class="flex items-start gap-3 py-2 border-b border-zinc-800/60">
        <div class="text-[10px] text-zinc-600 w-8 flex-shrink-0 pt-0.5">#${s.step}</div>
        <div class="text-${color}-400 flex-shrink-0 pt-0.5">${icon}</div>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="text-xs font-medium text-white font-mono">${esc(s.name)}</span>
            <span class="text-[10px] text-zinc-500">${fmtDur(s.duration_ms)}</span>
            ${!s.ok ? badge('failed', 'red') : ''}
          </div>
          <div class="text-[11px] mt-0.5">${detail}</div>
          ${args}${err}
          <div class="h-1 mt-1.5 rounded-full bg-${color}-500/50" style="width:${pct}%"></div>
        </div>
      </div>`;
    };

    container.innerHTML = `
      <button id="tr-back" class="text-xs text-zinc-400 hover:text-white mb-3 flex items-center gap-1">← Back to runs</button>
      <div class="flex items-center gap-2 flex-wrap mb-1">
        ${badge(run.source, SRC_COLOR[run.source] || 'zinc')}
        ${badge(run.status, STATUS_COLOR[run.status] || 'zinc')}
        ${run.escalation_count ? badge(`escalated ×${run.escalation_count}`, 'amber') : ''}
      </div>
      <h1 class="page-header">${esc(run.trigger || '(no trigger)')}</h1>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
        <div class="stat-card"><div class="text-[10px] text-zinc-500 uppercase">Duration</div><div class="text-lg font-semibold text-white">${fmtDur(run.duration_ms)}</div></div>
        <div class="stat-card"><div class="text-[10px] text-zinc-500 uppercase">Tokens</div><div class="text-lg font-semibold text-white">${fmtTokens(run.total_tokens)}</div></div>
        <div class="stat-card"><div class="text-[10px] text-zinc-500 uppercase">Model calls</div><div class="text-lg font-semibold text-white">${run.num_model_calls}</div></div>
        <div class="stat-card"><div class="text-[10px] text-zinc-500 uppercase">Tool calls</div><div class="text-lg font-semibold text-white">${run.num_tool_calls}</div></div>
      </div>
      <div class="text-[11px] text-zinc-500 mb-4 font-mono break-all">
        run ${esc(run.run_id)}${run.session_id ? ` · session ${esc(run.session_id)}` : ''}${run.model ? ` · ${esc(run.model)}` : ''} · started ${fmtAgo(run.started_at)}
      </div>
      ${run.result_preview ? `<div class="stat-card mb-4"><div class="text-[10px] text-zinc-500 uppercase mb-1">Outcome</div><div class="text-xs text-zinc-300 whitespace-pre-wrap">${esc(run.result_preview)}</div></div>` : ''}
      ${run.error ? `<div class="stat-card mb-4 border border-red-500/30"><div class="text-[10px] text-red-400 uppercase mb-1">Error</div><div class="text-xs text-red-300 break-all">${esc(run.error)}</div></div>` : ''}
      <h3 class="text-sm font-semibold text-white mb-2">Timeline (${spans.length} span${spans.length === 1 ? '' : 's'})</h3>
      <div class="stat-card">
        ${spans.length ? spans.map(spanRow).join('') : '<p class="text-zinc-500 text-sm">No spans recorded.</p>'}
      </div>
    `;
    document.getElementById('tr-back').addEventListener('click', () => render(container));
  }

  async function loadList(into) {
    let data, stats;
    try {
      [data, stats] = await Promise.all([
        api.get('/api/traces?limit=60'),
        api.get('/api/traces/stats'),
      ]);
    } catch (e) {
      if (into) into.innerHTML = `<p class="text-zinc-500 text-sm">Tracing API not available.</p>`;
      return;
    }
    const runs = data.runs || [];

    const statBar = `
      <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-5">
        <div class="stat-card"><div class="text-[10px] text-zinc-500 uppercase">Recent runs</div><div class="text-lg font-semibold text-white">${stats.recent_total || 0}</div></div>
        <div class="stat-card"><div class="text-[10px] text-zinc-500 uppercase">Active</div><div class="text-lg font-semibold text-blue-400">${stats.active || 0}</div></div>
        <div class="stat-card"><div class="text-[10px] text-zinc-500 uppercase">Errors</div><div class="text-lg font-semibold ${stats.errors ? 'text-red-400' : 'text-white'}">${stats.errors || 0}</div></div>
        <div class="stat-card"><div class="text-[10px] text-zinc-500 uppercase">Avg duration</div><div class="text-lg font-semibold text-white">${fmtDur(stats.avg_duration_ms)}</div></div>
        <div class="stat-card"><div class="text-[10px] text-zinc-500 uppercase">Tokens</div><div class="text-lg font-semibold text-white">${fmtTokens(stats.total_tokens)}</div></div>
      </div>`;

    const row = (r) => {
      const tools = (r.tools_used || []).slice(0, 6);
      const more = (r.tools_used || []).length - tools.length;
      return `<button class="tr-row w-full text-left stat-card mb-2 hover:border-zinc-600 transition" data-run="${esc(r.run_id)}">
        <div class="flex items-center justify-between gap-3">
          <div class="flex items-center gap-2 min-w-0">
            ${badge(r.source, SRC_COLOR[r.source] || 'zinc')}
            ${badge(r.status, STATUS_COLOR[r.status] || 'zinc')}
            <span class="text-sm text-white truncate">${esc(r.trigger || '(no trigger)')}</span>
          </div>
          <div class="text-[10px] text-zinc-500 flex-shrink-0">${fmtAgo(r.started_at)}</div>
        </div>
        <div class="flex items-center gap-3 mt-1.5 text-[10px] text-zinc-500 flex-wrap">
          <span>${fmtDur(r.duration_ms)}</span>
          <span>◆ ${r.num_model_calls} model</span>
          <span>▸ ${r.num_tool_calls} tool</span>
          <span>${fmtTokens(r.total_tokens)} tok</span>
          ${r.escalation_count ? `<span class="text-amber-400">esc ×${r.escalation_count}</span>` : ''}
          ${tools.length ? `<span class="text-zinc-600 font-mono">${tools.map(esc).join(' · ')}${more > 0 ? ` +${more}` : ''}</span>` : ''}
        </div>
      </button>`;
    };

    const html = statBar + (runs.length
      ? `<div id="tr-list">${runs.map(row).join('')}</div>`
      : '<div class="stat-card"><p class="text-zinc-500 text-sm">No runs traced yet. Send a chat message or wait for a cron job to fire.</p></div>');

    if (into) {
      into.innerHTML = html;
    }
    into.querySelectorAll('.tr-row').forEach(btn =>
      btn.addEventListener('click', () => showDetail(btn.dataset.run)));
  }

  container.innerHTML = `
    <div class="flex items-center justify-between">
      <h1 class="page-header">Run Traces</h1>
      <button id="tr-refresh" class="text-xs px-3 py-1.5 rounded bg-zinc-700/60 text-zinc-200 hover:bg-zinc-600">Refresh</button>
    </div>
    <p class="page-desc">Every agent invocation as one trace: the trigger, each model call (latency + tokens) and tool call (args + result), and the final outcome — all under one run ID.</p>
    <div id="tr-body"></div>
  `;
  const body = document.getElementById('tr-body');
  document.getElementById('tr-refresh').addEventListener('click', () => loadList(body));

  await loadList(body);

  // Self-cleaning auto-refresh: stops once the page is navigated away.
  _timer = setInterval(() => {
    if (!document.body.contains(body)) { clearInterval(_timer); _timer = null; return; }
    loadList(body);
  }, 5000);
}
