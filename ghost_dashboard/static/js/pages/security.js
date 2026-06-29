/** Security Audit page — AI-driven autonomous audit via evolve loop */

const t = (key, params) => window.GhostI18n?.t(key, params) ?? key;

let _auditEventSource = null;

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;

  if (_auditEventSource) { _auditEventSource.close(); _auditEventSource = null; }

  container.innerHTML = `
    <h1 class="page-header">${t('security.title')}</h1>
    <p class="page-desc">${t('security.subtitle')}</p>

    <div class="flex gap-3 mb-6 flex-wrap">
      <button id="btn-ai-audit" class="btn btn-primary">${t('security.runAudit')}</button>
      <button id="btn-stop-audit" class="btn btn-danger" style="display:none">${t('security.stopAudit')}</button>
    </div>

    <div id="audit-status" class="mb-4"></div>
    <div id="audit-steps" class="mb-4"></div>
    <div id="audit-result" class="mb-6"></div>

    <div id="key-posture-section" class="mb-6">
      <h2 class="text-base font-semibold text-white mb-3 flex items-center gap-2">
        <svg class="w-4 h-4 text-ghost-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
            d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/>
        </svg>
        ${t('security.keyPosture')}
      </h2>
      <div id="key-posture-content">
        <div class="flex items-center gap-2 text-sm text-zinc-500 animate-pulse">
          <svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
          </svg>
          <span>${t('security.analyzingPosture')}</span>
        </div>
      </div>
    </div>
  `;

  const aiBtn = document.getElementById('btn-ai-audit');
  const stopBtn = document.getElementById('btn-stop-audit');
  const statusEl = document.getElementById('audit-status');
  const stepsEl = document.getElementById('audit-steps');
  const resultEl = document.getElementById('audit-result');

  let activeSessionId = null;

  aiBtn.addEventListener('click', startAiAudit);

  // Fetch key posture on page load
  fetchKeyPosture();

  async function startAiAudit() {
    aiBtn.disabled = true;
    stopBtn.style.display = '';
    stepsEl.innerHTML = '';
    resultEl.innerHTML = '';
    statusEl.innerHTML = `
      <div class="flex items-center gap-2 text-sm text-amber-400 animate-pulse">
        <svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
        </svg>
        <span>${t('security.aiAuditing')}</span>
      </div>`;

    try {
      const resp = await api.post('/api/security/ai-audit', {});
      if (!resp.ok) {
        statusEl.innerHTML = `<div class="text-red-400 text-xs">${u.escapeHtml(resp.error || t('security.failedToStart'))}</div>`;
        resetButtons();
        return;
      }
      activeSessionId = resp.session_id;
      streamAiAudit(resp.session_id);
    } catch (err) {
      statusEl.innerHTML = `<div class="text-red-400 text-xs">${u.escapeHtml(err.message)}</div>`;
      resetButtons();
    }
  }

  function streamAiAudit(sessionId) {
    if (_auditEventSource) { _auditEventSource.close(); }

    let stepCount = 0;
    _auditEventSource = new EventSource(`/api/security/ai-audit/stream/${sessionId}`);

    _auditEventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'step') {
        stepCount++;
        const step = data.step;
        const preview = (step.result || '').substring(0, 150).replace(/\n/g, ' ');
        statusEl.innerHTML = `
          <div class="flex items-center gap-2 text-sm text-amber-400 animate-pulse">
            <svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
            </svg>
            <span>${t('security.stepN', {n: stepCount})}: ${u.escapeHtml(step.tool)}...</span>
          </div>`;

        const stepDiv = document.createElement('div');
        stepDiv.className = 'security-step';
        stepDiv.innerHTML = `
          <div class="flex items-center gap-2 text-[11px]">
            <span class="text-ghost-400 font-mono font-medium">${t('security.stepN', {n: step.step})}</span>
            <span class="text-zinc-400 font-mono">${u.escapeHtml(step.tool)}</span>
            <span class="text-zinc-600 ml-auto">${new Date(step.time).toLocaleTimeString()}</span>
          </div>
          ${preview ? `<div class="text-[10px] text-zinc-600 mt-0.5 truncate">${u.escapeHtml(preview)}</div>` : ''}
        `;
        stepsEl.appendChild(stepDiv);
        stepsEl.scrollTop = stepsEl.scrollHeight;
      }

      if (data.type === 'done') {
        _auditEventSource.close();
        _auditEventSource = null;
        activeSessionId = null;

        statusEl.innerHTML = `
          <div class="flex items-center gap-2 text-sm text-emerald-400">
            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>
            </svg>
            <span>${t('security.auditComplete')}</span>
            <span class="text-zinc-500 text-xs">${data.tools_used?.length || 0} ${t('security.toolsUsed')}, ${data.elapsed}s</span>
          </div>`;

        if (stepCount > 0) {
          collapseSteps(stepsEl, stepCount);
        }

        resultEl.innerHTML = `
          <div class="stat-card border border-ghost-500/20">
            <div class="flex items-center gap-2 mb-3">
              <svg class="w-5 h-5 text-ghost-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>
              </svg>
              <span class="text-sm font-semibold text-white">${t('security.aiReport')}</span>
            </div>
            <div class="prose-ghost text-xs leading-relaxed">${formatMarkdown(data.result || t('security.noReport'))}</div>
          </div>`;

        resetButtons();
      }

      if (data.type === 'error') {
        _auditEventSource.close();
        _auditEventSource = null;
        activeSessionId = null;

        statusEl.innerHTML = `<div class="text-red-400 text-xs">${t('security.auditError')} ${u.escapeHtml(data.error)}</div>`;
        resetButtons();
      }
    };

    _auditEventSource.onerror = () => {
      _auditEventSource.close();
      _auditEventSource = null;
      activeSessionId = null;
      statusEl.innerHTML = `<div class="text-amber-400 text-xs">${t('security.connectionLost')}</div>`;
    };
  }

  stopBtn.addEventListener('click', async () => {
    if (!activeSessionId) return;
    stopBtn.disabled = true;
    try {
      await api.post(`/api/security/ai-audit/stop/${activeSessionId}`);
      statusEl.innerHTML = `<div class="text-zinc-400 text-xs">${t('security.auditStopped')}</div>`;
    } catch { /* will resolve */ }
    resetButtons();
  });

  function resetButtons() {
    aiBtn.disabled = false;
    stopBtn.style.display = 'none';
    stopBtn.disabled = false;
  }

  async function fetchKeyPosture() {
    const postureEl = document.getElementById('key-posture-content');
    try {
      const resp = await api.post('/api/security/key-posture', {});
      if (!resp.ok) {
        postureEl.innerHTML = `<div class="text-red-400 text-xs">${t('security.failedToLoadPosture')} ${u.escapeHtml(resp.error || t('common.unknownError'))}</div>`;
        return;
      }
      renderKeyPosture(resp);
    } catch (err) {
      postureEl.innerHTML = `<div class="text-red-400 text-xs">${u.escapeHtml(err.message)}</div>`;
    }
  }

  function renderKeyPosture(data) {
    const postureEl = document.getElementById('key-posture-content');
    const { posture, finding_count, findings, summary, error } = data;

    if (error && !findings) {
      postureEl.innerHTML = `<div class="text-red-400 text-xs">${u.escapeHtml(error)}</div>`;
      return;
    }

    // Determine badge color based on posture
    const badgeClass = posture === 'green' ? 'badge-green' :
                       posture === 'yellow' ? 'badge-yellow' :
                       posture === 'red' ? 'badge-red' : 'badge-zinc';
    const badgeText = posture === 'green' ? t('security.healthy') :
                      posture === 'yellow' ? t('security.warning') :
                      posture === 'red' ? t('security.critical') : t('security.unknown');

    let findingsHtml = '';
    if (findings && findings.length > 0) {
      findingsHtml = findings.map(f => {
        const severityBadge = f.severity === 'critical' ? 'badge-red' :
                              f.severity === 'warning' ? 'badge-yellow' : 'badge-blue';

        let evidenceHtml = '';
        if (f.evidence && Object.keys(f.evidence).length > 0) {
          const evidenceItems = Object.entries(f.evidence)
            .map(([k, v]) => `<span class="text-zinc-500">${u.escapeHtml(k)}:</span> <span class="text-zinc-300">${Array.isArray(v) ? v.join(', ') : u.escapeHtml(String(v))}</span>`)
            .join('</div><div class="text-[10px]">');
          evidenceHtml = `<div class="mt-1 text-[10px]">${evidenceItems}</div>`;
        }

        return `
          <div class="posture-finding p-2 rounded bg-surface-700/50 border border-surface-600/30 mb-2">
            <div class="flex items-center gap-2 mb-1">
              <span class="badge ${severityBadge} text-[10px]">${f.severity}</span>
              <span class="text-xs font-medium text-white">${u.escapeHtml(f.title)}</span>
            </div>
            <div class="text-[11px] text-zinc-400 mb-1">${u.escapeHtml(f.check_id)}</div>
            ${f.remediation ? `<div class="text-[11px] text-zinc-500"><span class="text-ghost-400">${t('security.remediation')}</span> ${u.escapeHtml(f.remediation)}</div>` : ''}
            ${evidenceHtml}
          </div>
        `;
      }).join('');
    } else {
      findingsHtml = `<div class="text-sm text-emerald-400 flex items-center gap-2">
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
        </svg>
        ${t('security.noIssues')}
      </div>`;
    }

    postureEl.innerHTML = `
      <div class="stat-card border border-ghost-500/20">
        <div class="flex items-center justify-between mb-3">
          <div class="flex items-center gap-3">
            <span class="badge ${badgeClass}">${badgeText}</span>
            <span class="text-xs text-zinc-400">${finding_count} ${t('security.findings')}</span>
          </div>
          <div class="flex gap-2 text-[10px]">
            ${summary.critical > 0 ? `<span class="text-red-400">${summary.critical} ${t('security.critical')}</span>` : ''}
            ${summary.warning > 0 ? `<span class="text-amber-400">${summary.warning} ${t('security.warning')}</span>` : ''}
            ${summary.info > 0 ? `<span class="text-blue-400">${summary.info} ${t('security.info')}</span>` : ''}
          </div>
        </div>
        <div class="findings-list">
          ${findingsHtml}
        </div>
      </div>
    `;
  }
}


function collapseSteps(container, count) {
  const steps = container.querySelectorAll('.security-step');
  if (steps.length === 0) return;

  const summary = document.createElement('div');
  summary.className = 'security-steps-summary';
  summary.innerHTML = `
    <button class="security-steps-toggle">
      <svg class="w-3 h-3 transition-transform" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/>
      </svg>
      <span>${count} ${t('security.auditSteps')}</span>
    </button>
  `;
  container.insertBefore(summary, container.firstChild);
  steps.forEach(s => s.style.display = 'none');

  summary.querySelector('.security-steps-toggle').addEventListener('click', () => {
    const hidden = steps[0]?.style.display === 'none';
    steps.forEach(s => s.style.display = hidden ? '' : 'none');
    summary.querySelector('svg').style.transform = hidden ? 'rotate(90deg)' : '';
  });
}


function formatMarkdown(text) {
  if (!text) return '';
  const esc = window.GhostUtils?.escapeHtml || ((s) => s);
  let html = esc(text);

  html = html.replace(/```(\w*)\n([\s\S]*?)```/g,
    '<pre class="bg-surface-950 rounded p-2 my-2 overflow-x-auto text-[11px] font-mono"><code>$2</code></pre>');
  html = html.replace(/`([^`]+)`/g, '<code class="bg-ghost-500/10 text-ghost-400 px-1 rounded text-[11px]">$1</code>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong class="text-white">$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/^### (.+)$/gm, '<h4 class="text-sm font-semibold text-white mt-3 mb-1">$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3 class="text-base font-semibold text-white mt-3 mb-1">$1</h3>');
  html = html.replace(/^# (.+)$/gm, '<h2 class="text-lg font-bold text-white mt-3 mb-1">$1</h2>');
  html = html.replace(/^- (.+)$/gm, '<li class="ml-4 list-disc text-zinc-300">$1</li>');
  html = html.replace(/^\d+\.\s+(.+)$/gm, '<li class="ml-4 list-decimal text-zinc-300">$1</li>');
  html = html.replace(/\n{2,}/g, '</p><p class="mt-2">');
  html = '<p>' + html + '</p>';
  html = html.replace(/<p>\s*<\/p>/g, '');

  return html;
}
