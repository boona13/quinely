/** Overview page — Ghost command center */

const t = (key, params) => window.GhostI18n?.t(key, params) ?? key;

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;
  const [status, feed, usage] = await Promise.all([
    api.get('/api/status'),
    api.get('/api/feed'),
    api.get('/api/usage/live').catch(() => ({})),
  ]);

  const s = status;
  const running = s.running;
  const paused = s.paused;
  const statusLabel = paused ? t('status.paused') : running ? t('status.runningStatus') : t('status.stopped');
  const statusColor = paused ? 'amber' : running ? 'emerald' : 'red';
  const statusGlow = running && !paused ? 'box-shadow: 0 0 12px rgba(52, 211, 153, 0.3)' : '';

  let modelDisplay = s.model || '\u2014';
  let providerPart = '';
  let modelPart = modelDisplay;
  if (modelDisplay.includes(':')) {
    const idx = modelDisplay.indexOf(':');
    providerPart = modelDisplay.slice(0, idx);
    modelPart = modelDisplay.slice(idx + 1);
  }

  const allEntries = feed.entries || [];
  const recentEntries = allEntries.slice(0, 5);

  // Momentum: activity over the last 14 days + a run streak.
  const DAYS = 14;
  const dayCounts = buildDayBuckets(allEntries, DAYS);
  const streak = computeStreak(allEntries);
  const skillsCount = s.live?.skills ?? 0;
  const memoryCount = s.live?.memory_entries ?? 0;

  const sessionTokens = usage.session_tokens || s.session_tokens || 0;
  const sessionCalls = usage.calls_this_session || s.calls_this_session || 0;

  const featureKeys = ['tool_loop','memory','skills','plugins','browser','cron','vision','tts','security_audit','session_memory'];
  const featureLabels = { tool_loop:t('overview.featureToolLoop'), memory:t('overview.featureMemory'), skills:t('overview.featureSkills'), plugins:t('overview.featurePlugins'), browser:t('overview.featureBrowser'), cron:t('overview.featureCron'), vision:t('overview.featureVision'), tts:t('overview.featureTts'), security_audit:t('overview.featureSecurityAudit'), session_memory:t('overview.featureSessionMemory') };
  const allFeaturesOn = featureKeys.every(k => s.features?.[k]);

  container.innerHTML = `
    <!-- Status hero -->
    <div class="overview-hero-v2 mb-6">
      <div class="flex items-center gap-4 mb-4">
        <div class="status-orb status-orb-${statusColor}" style="${statusGlow}">
          <span class="status-orb-inner bg-${statusColor}-500 ${running && !paused ? 'animate-pulse' : ''}"></span>
        </div>
        <div class="flex-1 min-w-0">
          <div class="text-lg font-bold text-white leading-tight">${statusLabel}</div>
          <div class="flex items-center gap-1.5 mt-0.5">
            ${providerPart ? `<span class="text-xs text-zinc-500">${u.escapeHtml(providerPart)}</span><span class="text-xs text-zinc-700">/</span>` : ''}
            <span class="text-xs text-zinc-300 font-medium">${u.escapeHtml(modelPart)}</span>
            ${s.uptime_seconds ? `<span class="text-xs text-zinc-600 ml-2">${formatUptime(s.uptime_seconds)}</span>` : ''}
            ${s.secrets?.encrypted
              ? `<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 font-medium ml-2" title="API keys, tokens and credentials are encrypted at rest">&#128274; secrets encrypted</span>`
              : ''}
            ${s.sandbox?.enabled
              ? `<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-sky-500/15 text-sky-400 font-medium ml-2" title="Shell commands run with resource limits, env scrubbing and process-group kill${s.sandbox.rlimits_supported ? ' (POSIX rlimits active)' : ''}">&#128737; sandboxed</span>`
              : ''}
          </div>
        </div>
        <div class="flex gap-2 flex-shrink-0">
          <button id="btn-pause" class="btn btn-sm ${paused ? 'btn-primary' : 'btn-secondary'}">${paused ? t('overview.resume') : t('overview.pause')}</button>
          ${s.embedded ? `<button id="btn-reload" class="btn btn-sm btn-secondary">${t('overview.reload')}</button>` : ''}
          ${s.embedded ? `<button id="btn-restart" class="btn btn-sm btn-secondary">${t('overview.restart')}</button>` : ''}
          ${s.embedded ? `<button id="btn-shutdown" class="btn btn-sm btn-danger">${t('overview.shutdown')}</button>` : ''}
        </div>
      </div>
    </div>

    ${s.live ? `
    <!-- Metrics grid -->
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
      <div class="metric-card-v2" data-goto="feed">
        <div class="metric-card-icon-wrap bg-blue-500/10">
          <svg class="w-4 h-4 text-blue-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"/></svg>
        </div>
        <div>
          <div class="metric-card-label">${t('overview.sessionsToday')}</div>
          <div class="flex items-baseline gap-2">
            <div class="metric-card-value">${s.today_actions}</div>
            <div class="metric-card-sub">${s.total_actions} ${t('overview.allTime')}</div>
          </div>
        </div>
      </div>
      <div class="metric-card-v2">
        <div class="metric-card-icon-wrap bg-ghost-500/10">
          <svg class="w-4 h-4 text-ghost-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z"/></svg>
        </div>
        <div>
          <div class="metric-card-label">${t('overview.tokensSession')}</div>
          <div class="flex items-baseline gap-2">
            <div class="metric-card-value">${sessionTokens.toLocaleString()}</div>
            <div class="metric-card-sub">${sessionCalls} ${t('overview.llmCalls')}</div>
          </div>
        </div>
      </div>
      <div class="metric-card-v2" data-goto="skills">
        <div class="metric-card-icon-wrap bg-amber-500/10">
          <svg class="w-4 h-4 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
        </div>
        <div>
          <div class="metric-card-label">${t('overview.skills')}</div>
          <div class="flex items-baseline gap-2">
            <div class="metric-card-value">${s.live.skills}</div>
            <div class="metric-card-sub">${t('overview.skillsReady')}</div>
          </div>
        </div>
      </div>
      <div class="metric-card-v2" data-goto="memory">
        <div class="metric-card-icon-wrap bg-emerald-500/10">
          <svg class="w-4 h-4 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4"/></svg>
        </div>
        <div>
          <div class="metric-card-label">${t('overview.memory')}</div>
          <div class="flex items-baseline gap-2">
            <div class="metric-card-value">${s.live.memory_entries}</div>
            <div class="metric-card-sub">${t('overview.memoriesStored')}</div>
          </div>
        </div>
      </div>
    </div>
    ` : `<div class="mb-6 p-3 rounded-lg" style="background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.2)">
      <div class="text-xs text-amber-400">${t('overview.standaloneMode')}</div>
    </div>`}

    <!-- Momentum -->
    <div class="momentum-card stat-card mb-6">
      <div class="flex items-center justify-between gap-6 flex-wrap">
        <div class="flex-1 min-w-0" style="min-width:220px">
          <div class="text-sm text-zinc-200 leading-relaxed">
            ${buildStory({ today: s.today_actions ?? 0, total: s.total_actions ?? 0, skills: skillsCount, memory: memoryCount, u })}
          </div>
          <div class="flex items-center gap-4 mt-3">
            <div class="flex items-center gap-1.5">
              <span class="text-base">${streak > 0 ? '\uD83D\uDD25' : '\u2014'}</span>
              <span class="text-lg font-bold text-white tabular-nums">${streak}</span>
              <span class="text-[11px] text-zinc-500">day${streak === 1 ? '' : 's'} active</span>
            </div>
            <div class="h-4 w-px bg-zinc-700"></div>
            <div class="flex items-center gap-1.5">
              <span class="text-lg font-bold text-white tabular-nums">${dayCounts.reduce((a, b) => a + b, 0)}</span>
              <span class="text-[11px] text-zinc-500">actions / ${DAYS}d</span>
            </div>
          </div>
        </div>
        <div class="flex-shrink-0">
          <div class="text-[10px] uppercase tracking-wider text-zinc-600 mb-1.5 text-right">Activity \u00b7 ${DAYS} days</div>
          ${sparklineSVG(dayCounts)}
        </div>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-4 gap-6 mb-6">
      <!-- Quick actions -->
      <div>
        <h2 class="text-sm font-semibold text-zinc-400 mb-3">${t('overview.quickActions')}</h2>
        <div class="space-y-2">
          <div class="quick-action-v2" data-goto="chat">
            <div class="qa-icon bg-ghost-600/20"><svg class="w-4 h-4 text-ghost-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg></div>
            <div class="flex-1 min-w-0">
              <div class="text-sm font-medium text-zinc-200">${t('overview.qaChat')}</div>
              <div class="text-[11px] text-zinc-600">${t('overview.qaChatDesc')}</div>
            </div>
            <svg class="w-4 h-4 text-zinc-700 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
          </div>
          <div class="quick-action-v2" data-goto="skills">
            <div class="qa-icon bg-amber-500/10"><svg class="w-4 h-4 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg></div>
            <div class="flex-1 min-w-0">
              <div class="text-sm font-medium text-zinc-200">${t('overview.qaSkills')}</div>
              <div class="text-[11px] text-zinc-600">${t('overview.qaSkillsDesc')}</div>
            </div>
            <svg class="w-4 h-4 text-zinc-700 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
          </div>
          <div class="quick-action-v2" data-goto="memory">
            <div class="qa-icon bg-emerald-500/10"><svg class="w-4 h-4 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/></svg></div>
            <div class="flex-1 min-w-0">
              <div class="text-sm font-medium text-zinc-200">${t('overview.qaMemory')}</div>
              <div class="text-[11px] text-zinc-600">${t('overview.qaMemoryDesc')}</div>
            </div>
            <svg class="w-4 h-4 text-zinc-700 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
          </div>
          <div class="quick-action-v2" data-goto="evolve">
            <div class="qa-icon bg-blue-500/10"><svg class="w-4 h-4 text-blue-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg></div>
            <div class="flex-1 min-w-0">
              <div class="text-sm font-medium text-zinc-200">Evolution</div>
              <div class="text-[11px] text-zinc-600">Self-improvement history</div>
            </div>
            <svg class="w-4 h-4 text-zinc-700 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
          </div>
        </div>
      </div>

      <!-- System health -->
      ${s.live ? `
      <div>
        <h2 class="text-sm font-semibold text-zinc-400 mb-3">${t('overview.systemHealth')}</h2>
        <div class="stat-card">
          <div class="space-y-3">
            <div class="health-item">
              <span class="health-dot health-dot-ok"></span>
              <span class="text-xs text-zinc-300">${t('overview.healthModel')}</span>
              <span class="text-[10px] text-zinc-500 ml-auto font-mono truncate max-w-[120px]">${u.escapeHtml(modelPart)}</span>
            </div>
            <div class="health-item">
              <span class="health-dot ${s.live.cron_enabled === s.live.cron_jobs ? 'health-dot-ok' : 'health-dot-warn'}"></span>
              <span class="text-xs text-zinc-300">${t('overview.healthCron')}</span>
              <span class="text-[10px] text-zinc-500 ml-auto">${s.live.cron_enabled}/${s.live.cron_jobs}</span>
            </div>
            <div class="health-item">
              <span class="health-dot health-dot-ok"></span>
              <span class="text-xs text-zinc-300">${t('overview.healthMemory')}</span>
              <span class="text-[10px] text-zinc-500 ml-auto">${s.live.memory_entries} ${t('common.entries')}</span>
            </div>
            <div class="health-item">
              <span class="health-dot health-dot-ok"></span>
              <span class="text-xs text-zinc-300">${t('overview.healthTools')}</span>
              <span class="text-[10px] text-zinc-500 ml-auto">${s.live.tools} ${t('overview.registered')}</span>
            </div>
          </div>
        </div>

        ${!allFeaturesOn ? `
        <h2 class="text-sm font-semibold text-zinc-400 mt-4 mb-3">${t('overview.features')}</h2>
        <div class="flex flex-wrap gap-1.5" id="feature-toggles">
          ${featureKeys.map(k => {
            const on = s.features?.[k];
            return `<button data-feature="${k}" class="badge ${on ? 'badge-green' : 'badge-zinc'} cursor-pointer hover:opacity-80 text-[10px] px-2 py-0.5">
              ${on ? '\u25CF' : '\u25CB'} ${featureLabels[k]}
            </button>`;
          }).join('')}
        </div>
        ` : ''}
      </div>
      ` : ''}

      <!-- System Safety -->
      ${s.safety ? `
      <div>
        <h2 class="text-sm font-semibold text-zinc-400 mb-3">System Safety</h2>
        <div class="stat-card">
          <div class="space-y-3">
            <div class="text-[10px] uppercase tracking-wider text-zinc-600 mb-1">Output Guard</div>
            <div class="health-item">
              <span class="health-dot health-dot-ok"></span>
              <span class="text-xs text-zinc-300">Calls Processed</span>
              <span class="text-[10px] text-zinc-500 ml-auto font-mono">${s.safety.guard?.total_processed ?? 0}</span>
            </div>
            <div class="health-item">
              <span class="health-dot ${(s.safety.guard?.duplicates_removed ?? 0) > 0 ? 'health-dot-warn' : 'health-dot-ok'}"></span>
              <span class="text-xs text-zinc-300">Duplicates Caught</span>
              <span class="text-[10px] text-zinc-500 ml-auto font-mono">${s.safety.guard?.duplicates_removed ?? 0}</span>
            </div>
            <div class="health-item">
              <span class="health-dot ${(s.safety.guard?.calls_clamped ?? 0) > 0 ? 'health-dot-warn' : 'health-dot-ok'}"></span>
              <span class="text-xs text-zinc-300">Calls Clamped</span>
              <span class="text-[10px] text-zinc-500 ml-auto font-mono">${s.safety.guard?.calls_clamped ?? 0}</span>
            </div>
            <div class="text-[10px] uppercase tracking-wider text-zinc-600 mt-3 mb-1">Message Repair</div>
            <div class="health-item">
              <span class="health-dot health-dot-ok"></span>
              <span class="text-xs text-zinc-300">Messages Scanned</span>
              <span class="text-[10px] text-zinc-500 ml-auto font-mono">${s.safety.repair?.total_scanned ?? 0}</span>
            </div>
            <div class="health-item">
              <span class="health-dot ${(s.safety.repair?.dangling_found ?? 0) > 0 ? 'health-dot-warn' : 'health-dot-ok'}"></span>
              <span class="text-xs text-zinc-300">Dangling Repaired</span>
              <span class="text-[10px] text-zinc-500 ml-auto font-mono">${s.safety.repair?.dangling_found ?? 0}</span>
            </div>
          </div>
        </div>
      </div>
      ` : ''}

      <!-- Recent activity -->
      <div>
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold text-zinc-400">${t('overview.recentActivity')}</h2>
          <a href="#feed" class="text-[11px] text-ghost-400 hover:text-ghost-300 transition-colors">${t('overview.viewAll')} &rarr;</a>
        </div>
        <div class="space-y-2">
          ${recentEntries.length === 0 ? `<div class="text-xs text-zinc-600 py-8 text-center">${t('overview.noActivity')}</div>` :
            recentEntries.map(e => `
              <div class="feed-entry-compact type-${e.type || 'unknown'}">
                <div class="flex items-center gap-2">
                  <span class="text-xs flex-shrink-0">${u.TYPE_ICONS[e.type] || '\u2753'}</span>
                  <span class="text-xs text-zinc-400 truncate flex-1">${u.escapeHtml((e.result || '').slice(0, 100))}</span>
                  <span class="text-[10px] text-zinc-600 flex-shrink-0">${u.timeAgo(e.time)}</span>
                </div>
              </div>
            `).join('')}
        </div>
      </div>
    </div>
  `;

  // Navigation for metric cards and quick actions
  container.querySelectorAll('[data-goto]').forEach(el => {
    el.style.cursor = 'pointer';
    el.addEventListener('click', () => {
      window.location.hash = '#' + el.dataset.goto;
    });
  });

  document.getElementById('btn-pause')?.addEventListener('click', async () => {
    if (paused) await api.post('/api/ghost/resume');
    else await api.post('/api/ghost/pause');
    u.toast(paused ? t('overview.resumed') : t('overview.paused'));
    render(container);
  });

  document.getElementById('btn-reload')?.addEventListener('click', async () => {
    await api.post('/api/ghost/reload');
    u.toast(t('overview.configReloaded'));
    render(container);
  });

  document.getElementById('btn-restart')?.addEventListener('click', async () => {
    if (!confirm(t('overview.restartConfirm'))) return;
    try {
      await api.post('/api/ghost/restart');
    } catch {
      // Expected — server may die before response completes
    }
    u.toast(t('overview.restarting'));
    container.innerHTML = `<div class="flex flex-col items-center justify-center h-64 gap-4"><div class="animate-spin w-8 h-8 border-2 border-ghost-500 border-t-transparent rounded-full"></div><div class="text-zinc-400 text-sm">${t('overview.ghostRestarting')}</div><div class="text-zinc-600 text-xs">${t('overview.refreshAuto')}</div></div>`;
    const poll = setInterval(async () => {
      try {
        await api.get('/api/status');
        clearInterval(poll);
        u.toast(t('overview.ghostBack'));
        render(container);
      } catch {}
    }, 2000);
    setTimeout(() => clearInterval(poll), 30000);
  });

  document.getElementById('btn-shutdown')?.addEventListener('click', async () => {
    if (!confirm(t('overview.shutdownConfirm'))) return;
    const shutdownMsg = `<div class="flex flex-col items-center justify-center h-64 gap-4"><div class="text-zinc-400 text-lg font-semibold">${t('overview.ghostShutdown')}</div><div class="text-zinc-600 text-sm">${t('overview.toStartAgain')}</div><div class="font-mono text-sm text-ghost-400 bg-surface-800 px-4 py-2 rounded-lg">./start.sh</div><div class="text-zinc-600 text-xs">${t('overview.altStartCmd')}</div></div>`;
    try {
      const res = await api.post('/api/ghost/shutdown');
      u.toast(res.message || t('overview.shuttingDown'));
    } catch {
      // Expected — server dies before response completes
    }
    container.innerHTML = shutdownMsg;
  });

  document.getElementById('feature-toggles')?.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-feature]');
    if (!btn) return;
    const key = btn.dataset.feature;
    const configKey = `enable_${key}`;
    const current = s.features[key];
    await api.put('/api/config', { [configKey]: !current });
    u.toast(`${featureLabels[key]} ${!current ? t('common.enabled') : t('common.disabled')}`);
    render(container);
  });
}

function formatUptime(secs) {
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.floor(secs / 60) + 'm';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return h + 'h ' + m + 'm';
}

/** Day index (0 = local midnight epoch day) for an ISO timestamp. */
function dayIndex(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return null;
  const local = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  return Math.floor(local.getTime() / 86400000);
}

/** Counts of feed entries per day for the last `days` days (oldest → today). */
function buildDayBuckets(entries, days) {
  const todayIdx = dayIndex(new Date().toISOString());
  const counts = new Array(days).fill(0);
  for (const e of entries) {
    const idx = dayIndex(e.time);
    if (idx == null) continue;
    const offset = todayIdx - idx; // 0 = today
    if (offset >= 0 && offset < days) counts[days - 1 - offset] += 1;
  }
  return counts;
}

/** Consecutive days (ending today or yesterday) with at least one entry. */
function computeStreak(entries) {
  const todayIdx = dayIndex(new Date().toISOString());
  const active = new Set();
  for (const e of entries) {
    const idx = dayIndex(e.time);
    if (idx != null) active.add(idx);
  }
  if (active.size === 0) return 0;
  // Allow the streak to count from today, or from yesterday if nothing yet today.
  let cursor = active.has(todayIdx) ? todayIdx : (active.has(todayIdx - 1) ? todayIdx - 1 : null);
  if (cursor == null) return 0;
  let streak = 0;
  while (active.has(cursor)) { streak += 1; cursor -= 1; }
  return streak;
}

/** Inline SVG sparkline (rounded bars) for the day buckets. */
function sparklineSVG(counts) {
  const w = 176, h = 40, gap = 3;
  const n = counts.length || 1;
  const bw = (w - gap * (n - 1)) / n;
  const max = Math.max(1, ...counts);
  const bars = counts.map((c, i) => {
    const bh = Math.max(2, Math.round((c / max) * (h - 4)));
    const x = i * (bw + gap);
    const y = h - bh;
    const isToday = i === n - 1;
    const fill = c === 0 ? 'rgba(113,113,122,0.22)' : (isToday ? '#a78bfa' : 'rgba(167,139,250,0.55)');
    return `<rect x="${x.toFixed(1)}" y="${y}" width="${bw.toFixed(1)}" height="${bh}" rx="${Math.min(2, bw / 2).toFixed(1)}" fill="${fill}"><title>${c} action${c === 1 ? '' : 's'}</title></rect>`;
  }).join('');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Activity over the last ${n} days">${bars}</svg>`;
}

/** A short, human "what Ghost has been up to" line. */
function buildStory({ today, total, skills, memory, u }) {
  const esc = (n) => `<span class="text-white font-semibold">${n.toLocaleString()}</span>`;
  if (total === 0) {
    return `Ghost is warmed up and waiting. Give it a goal and watch it run \u2014 every action shows up here.`;
  }
  const lead = today > 0
    ? `Today Ghost has taken ${esc(today)} action${today === 1 ? '' : 's'}.`
    : `Ghost is idle right now, resting on ${esc(total)} lifetime action${total === 1 ? '' : 's'}.`;
  return `${lead} It's carrying ${esc(skills)} skill${skills === 1 ? '' : 's'} and ${esc(memory)} memor${memory === 1 ? 'y' : 'ies'}, ready to go.`;
}
