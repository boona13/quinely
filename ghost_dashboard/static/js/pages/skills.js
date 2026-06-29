/** Skills page — grouped, filterable, with enable/disable and requirements */

const t = (key, params) => window.GhostI18n?.t(key, params) ?? key;

let allSkills = [];
let expandedSkill = null;
let registrySkills = [];
let registryStats = null;

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;
  const data = await api.get('/api/skills');
  const stats = data.stats;
  const groups = data.groups;

  allSkills = [
    ...(groups.bundled || []),
    ...(groups.user || []),
    ...(groups.other || []),
  ];

  const countEl = document.getElementById('skills-count');
  if (countEl) countEl.textContent = stats.total;

  container.innerHTML = `
    <div class="flex items-center justify-between mb-1">
      <h1 class="page-header">${t('skills.title')}</h1>
      <div class="flex gap-2 items-center">
        <span class="badge badge-green">${stats.eligible} ${t('skills.eligible')}</span>
        ${stats.disabled ? `<span class="badge badge-zinc">${stats.disabled} ${t('skills.disabled')}</span>` : ''}
        ${stats.missing_reqs ? `<span class="badge badge-yellow">${stats.missing_reqs} ${t('skills.missingReqs')}</span>` : ''}
      </div>
    </div>
    <p class="page-desc">${stats.total} ${t('skills.subtitle')}</p>

    <!-- Tabs -->
    <div class="flex gap-1 mb-4 border-b border-zinc-800">
      <button id="tab-local" class="evo-tab active px-4 py-2 text-sm font-medium">${t('skills.localSkills')}</button>
      <button id="tab-registry" class="evo-tab px-4 py-2 text-sm font-medium">${t('skills.ghosthubRegistry')}</button>
    </div>

    <!-- Local Skills Panel -->
    <div id="panel-local">
      <div class="flex gap-3 mb-6">
        <input id="skills-search" type="text" class="form-input flex-1" placeholder="${t('skills.searchPlaceholder')}">
        <select id="skills-filter" class="form-input" style="width:150px">
          <option value="all">${t('skills.allSkills')}</option>
          <option value="eligible">${t('skills.eligible')}</option>
          <option value="disabled">${t('skills.disabled')}</option>
          <option value="missing">${t('skills.missingReqs')}</option>
        </select>
      </div>

      <div id="skills-groups"></div>

      <div class="mt-6 stat-card">
        <div class="text-xs text-zinc-500">
          <div>${t('skills.bundledDir')} <span class="font-mono text-zinc-400">${u.escapeHtml(data.bundled_dir)}</span></div>
          <div>${t('skills.userDir')} <span class="font-mono text-zinc-400">${u.escapeHtml(data.user_dir)}</span></div>
        </div>
      </div>
    </div>

    <!-- Registry Panel -->
    <div id="panel-registry" style="display:none">
      <div class="flex gap-3 mb-4">
        <input id="registry-search" type="text" class="form-input flex-1" placeholder="${t('skills.searchGhosthub')}">
        <button id="registry-refresh" class="btn btn-secondary btn-sm">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
          </svg>
        </button>
      </div>

      <div id="registry-stats" class="mb-4"></div>
      <div id="registry-results"></div>
    </div>
  `;

  renderGroups(groups, container, 'all', '', api, u);

  document.getElementById('skills-search')?.addEventListener('input', () => applyFilters(groups, container, api, u));
  document.getElementById('skills-filter')?.addEventListener('change', () => applyFilters(groups, container, api, u));

  // Tab switching
  document.getElementById('tab-local')?.addEventListener('click', () => switchTab('local'));
  document.getElementById('tab-registry')?.addEventListener('click', () => switchTab('registry'));

  // Registry search
  const registrySearch = document.getElementById('registry-search');
  let searchTimeout;
  registrySearch?.addEventListener('input', () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => searchRegistry(registrySearch.value, api, u), 300);
  });

  document.getElementById('registry-refresh')?.addEventListener('click', () => refreshRegistry(api, u));

  // Load registry stats in background
  loadRegistryStats(api, u);
}

function switchTab(tab) {
  document.getElementById('tab-local')?.classList.toggle('active', tab === 'local');
  document.getElementById('tab-registry')?.classList.toggle('active', tab === 'registry');
  document.getElementById('panel-local').style.display = tab === 'local' ? '' : 'none';
  document.getElementById('panel-registry').style.display = tab === 'registry' ? '' : 'none';
}

async function loadRegistryStats(api, u) {
  try {
    const stats = await api.get('/api/skills/registry/stats');
    if (stats.ok) {
      registryStats = stats;
      const el = document.getElementById('registry-stats');
      if (el) {
        el.innerHTML = `
          <div class="flex gap-2 text-xs">
            <span class="badge badge-purple">${stats.total_skills} ${t('skills.title').toLowerCase()}</span>
            <span class="badge badge-blue">${stats.unique_tags} ${t('skills.tags')}</span>
            <span class="badge badge-green">${stats.unique_authors} ${t('skills.authors')}</span>
          </div>
        `;
      }
    }
  } catch (e) {
    console.log('Registry stats unavailable');
  }
}

async function searchRegistry(query, api, u) {
  const resultsEl = document.getElementById('registry-results');
  if (!resultsEl) return;

  if (!query.trim()) {
    resultsEl.innerHTML = '<div class="text-sm text-zinc-500">' + t('skills.typeToSearch') + '</div>';
    return;
  }

  resultsEl.innerHTML = '<div class="text-sm text-zinc-500">' + t('skills.searching') + '</div>';

  try {
    const data = await api.get('/api/skills/registry/search?q=' + encodeURIComponent(query));
    if (!data.ok) {
      resultsEl.innerHTML = `<div class="text-sm text-red-400">${t('common.error')}: ${u.escapeHtml(data.error)}</div>`;
      return;
    }

    registrySkills = data.skills || [];

    if (registrySkills.length === 0) {
      resultsEl.innerHTML = '<div class="text-sm text-zinc-500">' + t('skills.noSkillsFound') + ' "' + u.escapeHtml(query) + '"</div>';
      return;
    }

    resultsEl.innerHTML = `
      <div class="text-xs text-zinc-500 mb-2">${t('skills.resultCount', {n: data.count})}</div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        ${registrySkills.map(s => renderRegistryCard(s, u)).join('')}
      </div>
    `;

    resultsEl.querySelectorAll('[data-registry-install]').forEach(btn => {
      btn.addEventListener('click', () => startSecureScanInstall(btn, api, u));
    });
  } catch (e) {
    resultsEl.innerHTML = `<div class="text-sm text-red-400">${t('skills.searchFailed', {error: u.escapeHtml(e.message)})}</div>`;
  }
}

function renderRegistryCard(s, u) {
  const installed = allSkills.some(local => local.name === s.name);

  return `
    <div class="stat-card">
      <div class="flex items-start justify-between mb-2">
        <div class="flex-1 min-w-0">
          <div class="font-semibold text-sm text-white truncate">${u.escapeHtml(s.name)}</div>
          <div class="text-xs text-zinc-400">${u.escapeHtml(s.author || t('common.unknown'))}</div>
        </div>
        <span class="text-[10px] text-zinc-500">v${u.escapeHtml(s.version || '0.0.0')}</span>
      </div>

      <div class="text-xs text-zinc-400 leading-relaxed mb-3">${u.escapeHtml(s.description || t('skills.noDescription'))}</div>

      <div class="flex flex-wrap gap-1 mb-3">
        ${(s.tags || []).slice(0, 4).map(tg =>
          '<span class="inline-block text-[10px] px-1.5 py-0.5 rounded bg-ghost-500/10 text-ghost-400 border border-ghost-500/20">'
          + u.escapeHtml(tg) + '</span>'
        ).join('')}
      </div>

      <div class="flex gap-2">
        <button class="btn btn-primary btn-sm flex-1" data-registry-install="${u.escapeHtml(s.name)}" ${installed ? 'disabled' : ''}>
          ${installed ? t('skills.installed') : t('common.install')}
        </button>
      </div>
    </div>
  `;
}

async function startSecureScanInstall(btn, api, u) {
  const name = btn.dataset.registryInstall;
  btn.disabled = true;
  btn.innerHTML = '<span class="animate-pulse">Scanning...</span>';

  try {
    const scan = await api.get('/api/skills/registry/' + encodeURIComponent(name) + '/scan');
    if (!scan.ok) {
      u.toast(scan.error || 'Scan failed', 'error');
      btn.disabled = false;
      btn.innerHTML = t('common.install');
      return;
    }

    const security = scan.security || {};
    const verdict = security.verdict || 'safe';
    const findings = security.findings || [];
    const counts = security.finding_counts || {};

    if (verdict === 'blocked') {
      btn.innerHTML = 'Blocked';
      btn.classList.add('opacity-50', 'btn-danger');
      btn.classList.remove('btn-primary');
      showSecurityModal(name, security, null, u);
      return;
    }

    if (verdict === 'safe') {
      await doInstall(name, btn, api, u);
      return;
    }

    showSecurityModal(name, security, async () => {
      closeSecurityModal();
      btn.innerHTML = '<span class="animate-pulse">Installing...</span>';
      await doInstall(name, btn, api, u);
    }, u);

  } catch (e) {
    u.toast('Scan failed: ' + e.message, 'error');
    btn.disabled = false;
    btn.innerHTML = t('common.install');
  }
}

async function doInstall(name, btn, api, u) {
  try {
    const result = await api.post('/api/skills/registry/' + encodeURIComponent(name) + '/install', {});
    if (result.ok || result.installed) {
      u.toast('Installed ' + name, 'success');
      btn.innerHTML = t('skills.installed');
      btn.classList.add('opacity-50');
      btn.disabled = true;

      const sec = result.security || {};
      if (sec.verdict === 'caution' || sec.verdict === 'dangerous') {
        u.toast('Note: this skill has ' + (sec.finding_counts?.high || 0) + ' security finding(s)', 'warning');
      }

      refreshLocalSkillsData(api);
    } else {
      const sec = result.security || {};
      if (sec.blocked) {
        btn.innerHTML = 'Blocked';
        btn.classList.add('opacity-50', 'btn-danger');
        btn.classList.remove('btn-primary');
        showSecurityModal(name, sec, null, u);
      } else {
        u.toast(result.error || t('skills.installFailed'), 'error');
        btn.disabled = false;
        btn.innerHTML = t('common.install');
      }
    }
  } catch (e) {
    u.toast(t('skills.installFailed') + ': ' + e.message, 'error');
    btn.disabled = false;
    btn.innerHTML = t('common.install');
  }
}

async function refreshLocalSkillsData(api) {
  try {
    const data = await api.get('/api/skills');
    const groups = data.groups;
    allSkills = [
      ...(groups.bundled || []),
      ...(groups.user || []),
      ...(groups.other || []),
    ];
    const countEl = document.getElementById('skills-count');
    if (countEl) countEl.textContent = data.stats.total;
  } catch (_) { /* best-effort */ }
}

function showSecurityModal(skillName, security, onProceed, u) {
  const existing = document.getElementById('security-scan-modal');
  if (existing) existing.remove();

  const verdict = security.verdict || 'unknown';
  const findings = security.findings || [];
  const score = security.risk_score || 0;
  const counts = security.finding_counts || {};

  const verdictColors = {
    blocked: 'text-red-400',
    dangerous: 'text-orange-400',
    caution: 'text-amber-400',
    safe: 'text-emerald-400',
  };
  const verdictIcons = {
    blocked: '&#x26D4;',
    dangerous: '&#x26A0;',
    caution: '&#x26A0;',
    safe: '&#x2705;',
  };
  const severityColors = {
    critical: 'bg-red-500/15 text-red-400 border-red-500/30',
    high: 'bg-orange-500/15 text-orange-400 border-orange-500/30',
    medium: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
    low: 'bg-zinc-500/15 text-zinc-400 border-zinc-500/30',
  };

  let findingsHtml = '';
  if (findings.length > 0) {
    findingsHtml = findings.map(f => `
      <div class="p-2 rounded border ${severityColors[f.severity] || severityColors.low} mb-2">
        <div class="flex items-center gap-2 mb-1">
          <span class="badge badge-${f.severity === 'critical' ? 'red' : f.severity === 'high' ? 'yellow' : 'zinc'} text-[10px]">${u.escapeHtml(f.severity)}</span>
          <span class="text-[10px] text-zinc-500">${u.escapeHtml(f.category)}</span>
        </div>
        <div class="text-xs">${u.escapeHtml(f.message)}</div>
        ${f.evidence ? '<div class="text-[10px] text-zinc-500 font-mono mt-1 truncate">' + u.escapeHtml(f.evidence) + '</div>' : ''}
      </div>
    `).join('');
  } else {
    findingsHtml = '<div class="text-xs text-zinc-500">No security issues found.</div>';
  }

  const overlay = document.createElement('div');
  overlay.id = 'security-scan-modal';
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-panel" style="max-width:560px">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-sm font-semibold text-white">Security Scan: ${u.escapeHtml(skillName)}</h3>
        <button id="btn-close-sec-modal" class="btn btn-ghost btn-sm">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
      </div>

      <div class="flex items-center gap-3 mb-4 p-3 rounded stat-card">
        <span class="text-2xl">${verdictIcons[verdict] || '?'}</span>
        <div>
          <div class="text-sm font-semibold ${verdictColors[verdict] || 'text-zinc-300'}">${verdict.toUpperCase()}</div>
          <div class="text-[10px] text-zinc-500">Risk score: ${score}/100</div>
        </div>
        <div class="flex gap-2 ml-auto">
          ${counts.critical ? '<span class="badge badge-red">' + counts.critical + ' critical</span>' : ''}
          ${counts.high ? '<span class="badge badge-yellow">' + counts.high + ' high</span>' : ''}
          ${counts.medium ? '<span class="badge badge-zinc">' + counts.medium + ' medium</span>' : ''}
        </div>
      </div>

      <div class="mb-4" style="max-height:280px;overflow-y:auto">
        ${findingsHtml}
      </div>

      ${verdict === 'blocked' ? `
        <div class="p-2 rounded bg-red-500/10 border border-red-500/20 text-xs text-red-400 mb-4">
          This skill has been blocked due to critical security findings. It cannot be installed.
        </div>
        <div class="flex justify-end">
          <button id="btn-sec-close" class="btn btn-secondary btn-sm">Close</button>
        </div>
      ` : `
        <div class="p-2 rounded bg-amber-500/10 border border-amber-500/20 text-xs text-amber-400 mb-4">
          This skill has security findings. Review them carefully before proceeding. Quinely's runtime safeguards (allowed_commands, allowed_roots) still apply.
        </div>
        <div class="flex gap-3 justify-end">
          <button id="btn-sec-cancel" class="btn btn-secondary btn-sm">Cancel</button>
          <button id="btn-sec-proceed" class="btn btn-danger btn-sm">Install Anyway</button>
        </div>
      `}
    </div>
  `;

  document.body.appendChild(overlay);

  overlay.querySelector('#btn-close-sec-modal')?.addEventListener('click', closeSecurityModal);
  overlay.querySelector('#btn-sec-close')?.addEventListener('click', closeSecurityModal);
  overlay.querySelector('#btn-sec-cancel')?.addEventListener('click', closeSecurityModal);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeSecurityModal(); });

  if (onProceed) {
    overlay.querySelector('#btn-sec-proceed')?.addEventListener('click', onProceed);
  }
}

function closeSecurityModal() {
  const m = document.getElementById('security-scan-modal');
  if (m) {
    m.classList.add('modal-closing');
    m.addEventListener('animationend', () => m.remove(), { once: true });
  }
}

async function refreshRegistry(api, u) {
  const btn = document.getElementById('registry-refresh');
  if (!btn) return;
  btn.disabled = true;
  btn.classList.add('animate-spin');

  try {
    const result = await api.post('/api/skills/registry/refresh', {});
    if (result.ok) {
      u.toast(result.message, 'success');
      loadRegistryStats(api, u);
    } else {
      u.toast(result.error || t('skills.refreshFailed'), 'error');
    }
  } catch (e) {
    u.toast(t('skills.refreshFailed') + ': ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.classList.remove('animate-spin');
  }
}

function applyFilters(groups, container, api, u) {
  const q = document.getElementById('skills-search').value.toLowerCase().trim();
  const filter = document.getElementById('skills-filter').value;
  renderGroups(groups, container, filter, q, api, u);
}

function filterSkills(skills, filter, query) {
  return skills.filter(s => {
    if (filter === 'eligible' && !s.eligible) return false;
    if (filter === 'disabled' && !s.disabled) return false;
    if (filter === 'missing' && !s.missing.bins.length && !s.missing.env.length) return false;
    if (query) {
      const hay = (s.name + ' ' + s.description + ' ' + (s.triggers || []).join(' ')).toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });
}

function renderGroups(groups, container, filter, query, api, u) {
  const target = document.getElementById('skills-groups');
  if (!target) return;

  const sections = [
    { key: 'bundled', label: t('skills.bundledSkills'), icon: '📦', skills: groups.bundled || [] },
    { key: 'user', label: t('skills.userSkills'), icon: '👤', skills: groups.user || [] },
    { key: 'other', label: t('skills.otherSkills'), icon: '📂', skills: groups.other || [] },
  ];

  let html = '';
  for (const sec of sections) {
    const filtered = filterSkills(sec.skills, filter, query);
    if (filtered.length === 0 && filter !== 'all') continue;

    html += `
      <div class="mb-6">
        <button class="group-toggle flex items-center gap-2 mb-3 cursor-pointer w-full text-left" data-group="${sec.key}">
          <svg class="w-4 h-4 text-zinc-500 transition-transform group-chevron" style="transform:rotate(90deg)" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/>
          </svg>
          <span class="text-sm">${sec.icon}</span>
          <span class="text-sm font-semibold text-zinc-300">${sec.label}</span>
          <span class="text-[10px] text-zinc-600">(${filtered.length})</span>
        </button>
        <div class="skills-grid grid grid-cols-1 md:grid-cols-2 gap-3" data-group-body="${sec.key}">
          ${filtered.map(s => renderSkillCard(s, u)).join('')}
          ${filtered.length === 0 ? '<div class="text-xs text-zinc-600 col-span-2 py-2">' + t('skills.noSkillsMatch') + '</div>' : ''}
        </div>
      </div>
    `;
  }

  target.innerHTML = html;

  target.querySelectorAll('.group-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.group;
      const body = target.querySelector('[data-group-body="' + key + '"]');
      const chev = btn.querySelector('.group-chevron');
      if (body.style.display === 'none') {
        body.style.display = '';
        chev.style.transform = 'rotate(90deg)';
      } else {
        body.style.display = 'none';
        chev.style.transform = 'rotate(0deg)';
      }
    });
  });

  target.querySelectorAll('.toggle[data-skill-toggle]').forEach(el => {
    el.addEventListener('click', async (e) => {
      e.stopPropagation();
      const name = el.dataset.skillToggle;
      const isOn = el.classList.contains('on');
      await api.put('/api/skills/' + name, { enabled: !isOn });
      el.classList.toggle('on');
      u.toast(name + ' ' + (isOn ? t('common.disabled') : t('common.enabled')));
    });
  });

  target.querySelectorAll('.skill-card[data-skill-name]').forEach(card => {
    card.addEventListener('click', async () => {
      const name = card.dataset.skillName;
      await openSkillDetail(name, api, u);
    });
  });
}

function renderSkillCard(s, u) {
  const hasMissing = s.missing.bins.length > 0 || s.missing.env.length > 0;

  let statusChips = '';
  statusChips += `<span class="badge badge-${s.source === 'bundled' ? 'blue' : 'purple'}">${s.source}</span>`;
  if (s.eligible) statusChips += '<span class="badge badge-green">' + t('skills.eligible') + '</span>';
  if (s.disabled) statusChips += '<span class="badge badge-zinc">' + t('skills.disabled') + '</span>';
  if (hasMissing) statusChips += '<span class="badge badge-yellow">' + t('skills.missingReqs') + '</span>';
  if (!s.os_ok) statusChips += '<span class="badge badge-red">' + t('skills.wrongOs') + '</span>';
  if (s.effective_model) statusChips += `<span class="badge badge-purple" title="${u.escapeHtml(s.effective_model)}">${t('skills.modelOverride')}</span>`;

  let reqsHtml = '';
  if (s.requirements.bins.length || s.requirements.env.length) {
    reqsHtml = '<div class="mt-2 flex flex-wrap gap-1">';
    for (const b of s.requirements.bins) {
      const ok = !s.missing.bins.includes(b);
      reqsHtml += '<span class="inline-flex items-center gap-1 text-[10px] font-mono px-1.5 py-0.5 rounded '
        + (ok ? 'bg-emerald-500/10 text-emerald-400' : 'bg-red-500/10 text-red-400') + '">'
        + (ok ? '✓' : '✗') + ' ' + u.escapeHtml(b) + '</span>';
    }
    for (const e of s.requirements.env) {
      const ok = !s.missing.env.includes(e);
      reqsHtml += '<span class="inline-flex items-center gap-1 text-[10px] font-mono px-1.5 py-0.5 rounded '
        + (ok ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400') + '">'
        + (ok ? '✓' : '⚠') + ' $' + u.escapeHtml(e) + '</span>';
    }
    reqsHtml += '</div>';
  }

  return `
    <div class="skill-card ${s.disabled ? 'opacity-50' : ''} ${hasMissing ? 'bis-2 bis-amber-40' : ''}" data-skill-name="${s.name}" style="${hasMissing ? 'border-inline-start: 2px solid rgba(245,158,11,0.4)' : ''}">
      <div class="flex items-start justify-between mb-2">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 mb-1">
            <span class="font-semibold text-sm text-white truncate">${u.escapeHtml(s.name)}</span>
            <span class="text-[10px] text-zinc-600">pri:${s.priority}</span>
          </div>
          <div class="text-xs text-zinc-400 leading-relaxed">${u.escapeHtml(s.description || t('skills.noDescription'))}</div>
        </div>
        <div class="toggle ${s.disabled ? '' : 'on'} ml-3 flex-shrink-0" data-skill-toggle="${s.name}">
          <span class="toggle-dot"></span>
        </div>
      </div>

      <div class="flex flex-wrap gap-1 mb-2">${statusChips}</div>

      <div class="flex flex-wrap gap-1 mb-1">
        ${(s.triggers || []).slice(0, 6).map(tg =>
          '<span class="inline-block text-[10px] px-1.5 py-0.5 rounded bg-ghost-500/10 text-ghost-400 border border-ghost-500/20">'
          + u.escapeHtml(tg) + '</span>'
        ).join('')}
        ${s.triggers.length > 6 ? '<span class="text-[10px] text-zinc-600">+' + (s.triggers.length - 6) + ' ' + t('common.more') + '</span>' : ''}
      </div>

      ${s.tools.length ? `<div class="flex flex-wrap gap-1 mb-1">
        ${s.tools.map(tl =>
          '<span class="inline-block text-[10px] px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/20">'
          + u.escapeHtml(tl) + '</span>'
        ).join('')}
      </div>` : ''}

      ${reqsHtml}
    </div>
  `;
}

function closeSkillModal() {
  const overlay = document.getElementById('skill-modal-overlay');
  if (!overlay) return;
  overlay.classList.add('modal-closing');
  overlay.addEventListener('animationend', () => overlay.remove(), { once: true });
  expandedSkill = null;
}

async function openSkillDetail(name, api, u) {
  const [data, modelOpts] = await Promise.all([
    api.get('/api/skills/' + name),
    api.get('/api/skills/model-options'),
  ]);
  if (data.error) { u.toast(data.error, 'error'); return; }

  expandedSkill = name;

  const hasMissing = data.missing.bins.length > 0 || data.missing.env.length > 0;

  let metaHtml = '<div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">';
  metaHtml += '<div><span class="text-zinc-500">' + t('skills.source') + '</span> <span class="text-zinc-300">' + data.source + '</span></div>';
  metaHtml += '<div><span class="text-zinc-500">' + t('skills.priority') + '</span> <span class="text-zinc-300">' + data.priority + '</span></div>';
  metaHtml += '<div><span class="text-zinc-500">' + t('common.status') + ':</span> <span class="' + (data.eligible ? 'text-emerald-400' : 'text-amber-400') + '">' + (data.eligible ? t('skills.eligible') : t('skills.notEligible')) + '</span></div>';
  metaHtml += '<div><span class="text-zinc-500">' + t('common.enabled') + ':</span> <span class="' + (data.disabled ? 'text-red-400' : 'text-emerald-400') + '">' + (data.disabled ? t('common.no') : t('common.yes')) + '</span></div>';
  metaHtml += '</div>';

  if (data.triggers.length) {
    metaHtml += '<div class="mt-3"><span class="text-[10px] text-zinc-500 font-semibold uppercase tracking-wider">' + t('skills.triggersLabel') + '</span><div class="flex flex-wrap gap-1 mt-1">'
      + data.triggers.map(tg => '<span class="inline-block text-[10px] px-1.5 py-0.5 rounded bg-ghost-500/10 text-ghost-400 border border-ghost-500/20">' + u.escapeHtml(tg) + '</span>').join('')
      + '</div></div>';
  }
  if (data.tools.length) {
    metaHtml += '<div class="mt-2"><span class="text-[10px] text-zinc-500 font-semibold uppercase tracking-wider">' + t('skills.toolsLabel') + '</span><div class="flex flex-wrap gap-1 mt-1">'
      + data.tools.map(tl => '<span class="inline-block text-[10px] px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/20">' + u.escapeHtml(tl) + '</span>').join('')
      + '</div></div>';
  }
  if (hasMissing) {
    metaHtml += '<div class="mt-3 p-2 rounded bg-amber-500/5 border border-amber-500/20">';
    metaHtml += '<span class="text-[10px] text-amber-400 font-semibold uppercase tracking-wider">' + t('skills.missingRequirementsLabel') + '</span>';
    if (data.missing.bins.length) metaHtml += '<div class="text-xs text-amber-300 mt-1">' + t('skills.binaries') + data.missing.bins.map(b => '<code class="font-mono">' + u.escapeHtml(b) + '</code>').join(', ') + '</div>';
    if (data.missing.env.length) metaHtml += '<div class="text-xs text-amber-300 mt-1">' + t('skills.envVars') + data.missing.env.map(e => '<code class="font-mono">$' + u.escapeHtml(e) + '</code>').join(', ') + '</div>';
    metaHtml += '</div>';
  }

  metaHtml += '<div class="mt-2 text-[10px] text-zinc-600 font-mono">' + u.escapeHtml(data.path) + '</div>';

  // Build model dropdown options
  const aliases = modelOpts.aliases || {};
  const defaultModel = modelOpts.default_model || '';
  const providers = modelOpts.providers || [];
  const currentValue = data.model_override || '';

  let modelSelectHtml = `
    <div class="skill-model-selector mt-4 mb-4">
      <label class="text-[10px] text-zinc-500 font-semibold uppercase tracking-wider block mb-1.5">${t('skills.modelSelectLabel')}</label>
      <p class="text-[10px] text-zinc-600 mb-2">${t('skills.modelSelectDesc')}</p>
      <select id="skill-model-select" class="skill-model-dropdown">
        <option value="">${t('skills.modelDefault')} (${u.escapeHtml(defaultModel)})</option>
        <optgroup label="${t('skills.modelAliasesGroup')}">`;

  for (const [alias, resolved] of Object.entries(aliases)) {
    const shortModel = resolved.split('/').slice(-1)[0];
    const selected = currentValue === resolved ? ' selected' : '';
    modelSelectHtml += `<option value="${u.escapeHtml(resolved)}"${selected}>${alias} (${u.escapeHtml(shortModel)})</option>`;
  }
  modelSelectHtml += '</optgroup>';

  for (const p of providers) {
    modelSelectHtml += `<optgroup label="${u.escapeHtml(p.id)}">`;
    for (const m of p.models) {
      const fullId = p.id + '/' + m;
      const selected = currentValue === fullId ? ' selected' : '';
      modelSelectHtml += `<option value="${u.escapeHtml(fullId)}"${selected}>${u.escapeHtml(m)}</option>`;
    }
    modelSelectHtml += '</optgroup>';
  }
  modelSelectHtml += '</select>';

  if (data.model && !data.model_override) {
    modelSelectHtml += `<div class="text-[10px] text-purple-400 mt-1.5">${t('skills.modelFromFrontmatter')} <span class="font-mono">${u.escapeHtml(data.model)}</span></div>`;
  }
  modelSelectHtml += '</div>';

  const existing = document.getElementById('skill-modal-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'skill-modal-overlay';
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-panel" style="max-width: 720px;">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-sm font-semibold text-white">${u.escapeHtml(name)}</h3>
        <button id="btn-close-skill-modal" class="btn btn-ghost btn-sm" title="${t('common.close')}">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
      </div>
      <div class="mb-4">${metaHtml}</div>
      ${modelSelectHtml}
      <textarea id="detail-editor" class="editor-textarea" style="min-height:320px">${u.escapeHtml(data.content)}</textarea>
      <div class="flex gap-3 mt-4 justify-end">
        <button id="btn-cancel-skill-modal" class="btn btn-secondary btn-sm">${t('common.cancel')}</button>
        <button id="btn-save-skill" class="btn btn-primary btn-sm">${t('skills.saveChanges')}</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeSkillModal();
  });

  overlay.querySelector('#btn-close-skill-modal').addEventListener('click', closeSkillModal);
  overlay.querySelector('#btn-cancel-skill-modal').addEventListener('click', closeSkillModal);

  overlay.querySelector('#btn-save-skill').addEventListener('click', async () => {
    if (!expandedSkill) return;
    const content = overlay.querySelector('#detail-editor').value;
    const selectedModel = overlay.querySelector('#skill-model-select').value;
    const payload = { content, model: selectedModel };
    const result = await api.put('/api/skills/' + expandedSkill, payload);
    if (result.error) {
      u.toast(result.error, 'error');
      return;
    }
    u.toast(t('skills.skillSaved'));
    closeSkillModal();
  });

  document.addEventListener('keydown', function escHandler(e) {
    if (e.key === 'Escape') {
      closeSkillModal();
      document.removeEventListener('keydown', escHandler);
    }
  });
}
