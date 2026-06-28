/** Ghost Dashboard — Main app router */

import { toast } from './utils.js';
import { i18n } from './i18n/index.js';
import { render as overview } from './pages/overview.js';
import { render as models } from './pages/models.js';
import { render as config } from './pages/config.js';
import { render as soul } from './pages/soul.js';
import { render as user } from './pages/user.js';
import { render as skills } from './pages/skills.js';
import { render as cron } from './pages/cron.js';
import { render as memory } from './pages/memory.js';
import { render as feed } from './pages/feed.js';
import { render as evolve } from './pages/evolve.js';
import { render as chat } from './pages/chat.js';
import { render as integrations } from './pages/integrations.js';
import { render as mcp } from './pages/mcp.js';
import { render as autonomy } from './pages/autonomy.js';
import { render as setup } from './pages/setup.js';
import { render as security } from './pages/security.js';
import { render as console_page } from './pages/console.js';
import { render as channels } from './pages/channels.js';
import { render as future_features } from './pages/future_features.js';
import { render as webhooks } from './pages/webhooks.js';
import { render as projects } from './pages/projects.js';
import { render as prs } from './pages/prs.js';
import { render as nodes } from './pages/nodes.js';
import { render as gallery } from './pages/gallery.js';
import { render as audit } from './pages/audit.js';
import { render as evolve_theater } from './pages/evolve_theater.js';
import { render as tools } from './pages/tools.js';
import { render as structured_memory } from './pages/structured_memory.js';
import { render as goals } from './pages/goals.js';
import { render as traces } from './pages/traces.js';
// Consolidated hubs — compose the pages above as tabs (see *_hub.js / identity / activity / evolution).
import { render as identity } from './pages/identity.js';
import { render as memory_hub } from './pages/memory_hub.js';
import { render as nodes_hub } from './pages/nodes_hub.js';
import { render as security_hub } from './pages/security_hub.js';
import { render as activity } from './pages/activity.js';
import { render as evolution } from './pages/evolution.js';
const pages = { overview, chat, models, config, soul, user, skills, cron, memory, feed, evolve, integrations, mcp, autonomy, setup, security, console: console_page, channels, future_features, webhooks, projects, prs, nodes, gallery, audit, evolve_theater, tools, structured_memory, goals, traces, identity, memory_hub, nodes_hub, security_hub, activity, evolution };
const container = document.getElementById('page-content');
let currentPage = null;
let pollTimer = null;
let needsSetup = false;
let _prevConnected = true;

function navigate(page) {
  page = page.split('?')[0];
  if (needsSetup && page !== 'setup') page = 'setup';
  if (!pages[page]) page = 'overview';
  currentPage = page;

  document.querySelectorAll('.nav-link').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
  });

  if (typeof _expandSectionForPage === 'function') _expandSectionForPage(page);

  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }

  if (page === 'chat') {
    container.classList.add('chat-active');
  } else {
    container.classList.remove('chat-active');
  }

  container.innerHTML = `<div class="flex items-center justify-center h-32 text-zinc-600"><div class="animate-pulse">${i18n.t('common.loading')}</div></div>`;

  pages[page](container).catch(err => {
    container.innerHTML = `<div class="text-red-400 p-4">${i18n.t('common.error')}: ${err.message}</div>`;
  });

  if (page === 'feed') {
    pollTimer = setInterval(() => pages.feed(container), 5000);
  }
}

document.querySelectorAll('.nav-link').forEach(el => {
  el.addEventListener('click', (e) => {
    e.preventDefault();
    const page = el.dataset.page;
    window.location.hash = page;
    navigate(page);
  });
});

/* ── Sidebar collapse/expand ─────────────────────────────────── */
const sidebar = document.getElementById('sidebar');
const sidebarToggle = document.getElementById('sidebar-toggle');

if (localStorage.getItem('ghost-sidebar-collapsed') === '1') {
  sidebar.classList.add('collapsed');
}

sidebarToggle.addEventListener('click', () => {
  sidebar.classList.toggle('collapsed');
  const isCollapsed = sidebar.classList.contains('collapsed');
  localStorage.setItem('ghost-sidebar-collapsed', isCollapsed ? '1' : '0');
  sidebarToggle.title = isCollapsed ? i18n.t('sidebar.expand') : i18n.t('sidebar.collapse');
});

/* ── Collapsible nav sections ────────────────────────────────── */
document.querySelectorAll('.nav-section-toggle').forEach(btn => {
  const sectionId = btn.dataset.section;
  const items = document.querySelector(`[data-section-id="${sectionId}"]`);
  if (!items) return;

  const saved = localStorage.getItem(`ghost-nav-${sectionId}`);
  if (saved === 'collapsed') {
    items.classList.add('collapsed');
    btn.dataset.collapsed = 'true';
  } else if (saved === 'expanded') {
    items.classList.remove('collapsed');
    btn.dataset.collapsed = 'false';
  }

  btn.addEventListener('click', () => {
    const isCollapsed = items.classList.toggle('collapsed');
    btn.dataset.collapsed = isCollapsed ? 'true' : 'false';
    localStorage.setItem(`ghost-nav-${sectionId}`, isCollapsed ? 'collapsed' : 'expanded');
  });
});

function _expandSectionForPage(page) {
  const link = document.querySelector(`.nav-link[data-page="${page}"]`);
  if (!link) return;
  const sectionItems = link.closest('.nav-section-items');
  if (sectionItems && sectionItems.classList.contains('collapsed')) {
    sectionItems.classList.remove('collapsed');
    const sectionId = sectionItems.dataset.sectionId;
    const toggle = document.querySelector(`.nav-section-toggle[data-section="${sectionId}"]`);
    if (toggle) toggle.dataset.collapsed = 'false';
  }
}

/* ── Command palette ─────────────────────────────────────────── */
const cmdPalette = document.getElementById('cmd-palette');
const cmdInput = document.getElementById('cmd-palette-input');
const cmdResults = document.getElementById('cmd-palette-results');
const cmdTrigger = document.getElementById('cmd-palette-trigger');
let cmdActiveIndex = 0;
let cmdFilteredItems = [];

const CMD_PAGES = [
  { page: 'chat', label: 'Chat', section: '' },
  { page: 'overview', label: 'Overview', section: '' },
  { page: 'activity', label: 'Activity', section: 'Monitor' },
  { page: 'identity', label: 'Identity', section: 'Intelligence' },
  { page: 'memory_hub', label: 'Memory', section: 'Intelligence' },
  { page: 'projects', label: 'Projects', section: 'Intelligence' },
  { page: 'models', label: 'Models', section: 'Intelligence' },
  { page: 'skills', label: 'Skills', section: 'Capabilities' },
  { page: 'tools', label: 'Tools', section: 'Capabilities' },
  { page: 'nodes_hub', label: 'AI Nodes', section: 'Capabilities' },
  { page: 'evolution', label: 'Evolution', section: 'Capabilities' },
  { page: 'goals', label: 'Goals', section: 'Capabilities' },
  { page: 'channels', label: 'Channels', section: 'Connections' },
  { page: 'webhooks', label: 'Webhooks', section: 'Connections' },
  { page: 'integrations', label: 'Integrations', section: 'Connections' },
  { page: 'mcp', label: 'MCP Servers', section: 'Connections' },
  { page: 'config', label: 'Configuration', section: 'System' },
  { page: 'cron', label: 'Cron Jobs', section: 'System' },
  { page: 'security_hub', label: 'Security', section: 'System' },
];

function openCmdPalette() {
  cmdPalette.style.display = '';
  cmdInput.value = '';
  cmdActiveIndex = 0;
  renderCmdResults('');
  requestAnimationFrame(() => cmdInput.focus());
}

function closeCmdPalette() {
  cmdPalette.style.display = 'none';
}

function renderCmdResults(query) {
  const q = query.toLowerCase().trim();
  cmdFilteredItems = q
    ? CMD_PAGES.filter(p => p.label.toLowerCase().includes(q) || p.section.toLowerCase().includes(q) || p.page.includes(q))
    : CMD_PAGES;
  if (cmdActiveIndex >= cmdFilteredItems.length) cmdActiveIndex = 0;

  cmdResults.innerHTML = cmdFilteredItems.map((item, i) => `
    <div class="cmd-result ${i === cmdActiveIndex ? 'active' : ''}" data-page="${item.page}">
      <span class="cmd-result-icon">
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5l7 7-7 7"/></svg>
      </span>
      <span>${item.label}</span>
      ${item.section ? `<span class="cmd-result-section">${item.section}</span>` : ''}
    </div>
  `).join('') || '<div class="text-xs text-zinc-600 text-center py-4">No matching pages</div>';

  cmdResults.querySelectorAll('.cmd-result').forEach(el => {
    el.addEventListener('click', () => {
      closeCmdPalette();
      window.location.hash = '#' + el.dataset.page;
      navigate(el.dataset.page);
    });
  });
}

if (cmdTrigger) {
  cmdTrigger.addEventListener('click', openCmdPalette);
}

if (cmdPalette) {
  cmdPalette.querySelector('.cmd-palette-backdrop').addEventListener('click', closeCmdPalette);

  cmdInput.addEventListener('input', () => {
    cmdActiveIndex = 0;
    renderCmdResults(cmdInput.value);
  });

  cmdInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeCmdPalette(); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); cmdActiveIndex = Math.min(cmdActiveIndex + 1, cmdFilteredItems.length - 1); renderCmdResults(cmdInput.value); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); cmdActiveIndex = Math.max(cmdActiveIndex - 1, 0); renderCmdResults(cmdInput.value); return; }
    if (e.key === 'Enter' && cmdFilteredItems[cmdActiveIndex]) {
      e.preventDefault();
      closeCmdPalette();
      const item = cmdFilteredItems[cmdActiveIndex];
      window.location.hash = '#' + item.page;
      navigate(item.page);
    }
  });
}

document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault();
    if (cmdPalette.style.display === 'none') openCmdPalette();
    else closeCmdPalette();
  }
  if (e.key === 'Escape' && cmdPalette.style.display !== 'none') {
    closeCmdPalette();
  }
});

async function updateSidebarStatus() {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  try {
    const s = await window.GhostAPI.get('/api/status');
    if (s.running && !s.paused) {
      dot.className = 'w-2 h-2 rounded-full bg-emerald-500 animate-pulse';
      text.textContent = i18n.t('status.runningPid', {pid: s.pid});
    } else if (s.paused) {
      dot.className = 'w-2 h-2 rounded-full bg-amber-500';
      text.textContent = i18n.t('status.paused');
    } else {
      dot.className = 'w-2 h-2 rounded-full bg-zinc-600';
      text.textContent = i18n.t('status.stopped');
    }
    if (!_prevConnected) {
      _prevConnected = true;
      window.dispatchEvent(new CustomEvent('ghost:restarted'));
      toast(i18n.t('status.ghostRestartedSuccess'));
    }
  } catch {
    if (_prevConnected) {
      _prevConnected = false;
      dot.className = 'w-2 h-2 rounded-full bg-amber-500 ghost-restart-pulse';
      text.textContent = i18n.t('status.restarting');
    }
  }
  try {
    const a = await window.GhostAPI.get('/api/autonomy/actions');
    const badge = document.getElementById('action-items-count');
    if (badge && a.pending_count > 0) {
      badge.textContent = a.pending_count;
      badge.classList.remove('hidden');
    } else if (badge) {
      badge.classList.add('hidden');
    }
  } catch {}
}

async function init() {
  await i18n.init();

  const langSelector = document.getElementById('lang-selector');
  if (langSelector) {
    langSelector.value = i18n.getLocale();
    langSelector.addEventListener('change', () => {
      i18n.setLocale(langSelector.value);
    });
  }

  i18n.onChange(() => {
    if (currentPage && pages[currentPage]) {
      pages[currentPage](container).catch(() => {});
    }
  });

  try {
    const setupStatus = await window.GhostAPI.get('/api/setup/status');
    if (setupStatus.needs_setup) {
      needsSetup = true;
      document.getElementById('sidebar').classList.add('hidden');
      navigate('setup');
      return;
    }
  } catch {}

  const initPage = (window.location.hash || '#chat').slice(1);
  navigate(initPage);
  updateSidebarStatus();
  setInterval(updateSidebarStatus, 5000);
  
  // Start usage status polling
  updateUsageStatus();
  setInterval(updateUsageStatus, 3000);
}

window.GhostI18n = i18n;

init();

window.addEventListener('hashchange', () => {
  navigate(window.location.hash.slice(1));
});

/* ── Usage Status Bar ─────────────────────────────────────────── */
let _cachedStatusModel = null;
async function updateUsageStatus() {
  const providerEl = document.getElementById('status-provider');
  const modelEl = document.getElementById('status-model');
  const activeDotEl = document.getElementById('status-active-dot');
  const tokensEl = document.getElementById('status-tokens');

  if (!providerEl || !modelEl || !tokensEl) return;

  try {
    const usage = await window.GhostAPI.get('/api/usage/live');

    let provider = usage.provider || '';
    let model = usage.model || '';

    if (!provider || !model) {
      if (!_cachedStatusModel) {
        try {
          const st = await window.GhostAPI.get('/api/status');
          _cachedStatusModel = st.model || '';
        } catch { _cachedStatusModel = ''; }
      }
      if (_cachedStatusModel && _cachedStatusModel.includes(':')) {
        const parts = _cachedStatusModel.split(':');
        provider = provider || parts[0];
        model = model || parts.slice(1).join(':');
      } else if (_cachedStatusModel) {
        model = model || _cachedStatusModel;
      }
    } else {
      _cachedStatusModel = null;
    }

    providerEl.textContent = provider || '\u2014';
    modelEl.textContent = model || '\u2014';
    tokensEl.textContent = usage.session_tokens?.toLocaleString() || '0';

    if (usage.active) {
      activeDotEl?.classList.remove('hidden');
    } else {
      activeDotEl?.classList.add('hidden');
    }
  } catch (err) {
    providerEl.textContent = '\u2014';
    modelEl.textContent = '\u2014';
    activeDotEl?.classList.add('hidden');
  }
}
