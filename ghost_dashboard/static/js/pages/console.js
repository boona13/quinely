/** Quinely Console — real-time terminal event stream */

const t = (key, params) => window.GhostI18n?.t(key, params) ?? key;

const CATEGORY_COLORS = {
  tool_call: { badge: 'bg-cyan-500/20 text-cyan-400', label: 'TOOL' },
  cron:      { badge: 'bg-yellow-500/20 text-yellow-400', label: 'CRON' },
  chat:      { badge: 'bg-emerald-500/20 text-emerald-400', label: 'CHAT' },
  channel:   { badge: 'bg-blue-500/20 text-blue-400', label: 'CHAN' },
  growth:    { badge: 'bg-purple-500/20 text-purple-400', label: 'GROW' },
  system:    { badge: 'bg-zinc-500/20 text-zinc-400', label: 'SYS' },
  error:     { badge: 'bg-red-500/20 text-red-400', label: 'ERR' },
};

const LEVEL_COLORS = {
  info:    'text-zinc-300',
  warn:    'text-yellow-400',
  error:   'text-red-400',
  success: 'text-emerald-400',
  debug:   'text-zinc-500',
};

const MAX_LINES = 2000;

let eventSource = null;
let autoScroll = true;
let paused = false;
let filters = { tool_call: true, cron: true, chat: true, channel: true, growth: true, system: true, error: true };
let searchTerm = '';
let eventCount = 0;
let logEl = null;

function formatTime(isoStr) {
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return '??:??:??'; }
}

function escapeHtml(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function matchesSearch(evt) {
  if (!searchTerm) return true;
  const q = searchTerm.toLowerCase();
  return (evt.title || '').toLowerCase().includes(q)
    || (evt.detail || '').toLowerCase().includes(q)
    || (evt.result || '').toLowerCase().includes(q)
    || (evt.category || '').toLowerCase().includes(q);
}

function buildLine(evt) {
  const cat = CATEGORY_COLORS[evt.category] || CATEGORY_COLORS.system;
  const levelColor = LEVEL_COLORS[evt.level] || LEVEL_COLORS.info;

  const ts = formatTime(evt.ts);
  const badge = cat.label;
  const title = escapeHtml(evt.title || '');
  const detail = escapeHtml(evt.detail || '');
  const result = evt.result ? escapeHtml(evt.result) : '';
  const dur = evt.duration_ms != null ? ` <span class="text-zinc-600">${evt.duration_ms}ms</span>` : '';

  let line = `<div class="console-line flex gap-0 leading-relaxed hover:bg-white/[0.02] px-3 py-0.5" data-category="${evt.category}">`;
  line += `<span class="text-zinc-600 w-[70px] flex-shrink-0 select-none">${ts}</span>`;
  line += `<span class="w-[52px] flex-shrink-0 text-center"><span class="text-[10px] font-semibold px-1.5 py-0.5 rounded ${cat.badge}">${badge}</span></span>`;
  line += `<span class="flex-1 min-w-0">`;
  line += `<span class="${levelColor} font-medium">${title}</span>`;
  if (detail) line += `  <span class="text-zinc-500">${detail}</span>`;
  if (dur) line += dur;
  if (result) line += `<div class="text-zinc-600 text-[11px] truncate pl-0 mt-0">  → ${result}</div>`;
  line += `</span>`;
  line += `</div>`;
  return line;
}

function appendEvent(evt) {
  if (!filters[evt.category]) return;
  if (!matchesSearch(evt)) return;

  eventCount++;
  const counter = document.getElementById('console-count');
  if (counter) counter.textContent = eventCount.toLocaleString();

  if (!logEl) return;

  logEl.insertAdjacentHTML('beforeend', buildLine(evt));

  while (logEl.children.length > MAX_LINES) {
    logEl.removeChild(logEl.firstChild);
  }

  if (autoScroll) {
    logEl.scrollTop = logEl.scrollHeight;
  }
}

function rebuildLog(events) {
  if (!logEl) return;
  eventCount = 0;
  const html = [];
  for (const evt of events) {
    if (!filters[evt.category]) continue;
    if (!matchesSearch(evt)) continue;
    html.push(buildLine(evt));
    eventCount++;
  }
  logEl.innerHTML = html.join('');
  const counter = document.getElementById('console-count');
  if (counter) counter.textContent = eventCount.toLocaleString();
  if (autoScroll) logEl.scrollTop = logEl.scrollHeight;
}

function connectSSE() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }

  eventSource = new EventSource('/api/console/stream');

  eventSource.onmessage = (e) => {
    if (paused) return;
    try {
      const evt = JSON.parse(e.data);
      appendEvent(evt);
    } catch {}
  };

  eventSource.onerror = () => {
    eventSource.close();
    eventSource = null;
    setTimeout(connectSSE, 3000);
  };
}

function disconnectSSE() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

let _allEvents = [];

export async function render(container) {
  const { GhostAPI: api } = window;

  container.innerHTML = `
    <div class="flex flex-col" style="height: calc(100vh - 2rem);">
      <!-- Header -->
      <div class="flex items-center justify-between mb-3">
        <div>
          <h1 class="text-lg font-bold text-white flex items-center gap-2">
            <svg class="w-5 h-5 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
            ${t('console.title')}
          </h1>
          <p class="text-xs text-zinc-500 mt-0.5">${t('console.subtitle')}</p>
        </div>
        <div class="flex items-center gap-2">
          <span id="console-count" class="text-xs text-zinc-600 tabular-nums font-mono">0</span>
          <span class="text-zinc-700 text-xs">${t('console.events')}</span>
        </div>
      </div>

      <!-- Toolbar -->
      <div class="flex flex-wrap items-center gap-2 mb-2 px-1">
        <div class="flex flex-wrap items-center gap-1.5">
          ${Object.entries(CATEGORY_COLORS).map(([cat, cfg]) => `
            <label class="flex items-center gap-1 cursor-pointer select-none group">
              <input type="checkbox" class="console-filter accent-violet-500 w-3 h-3" data-cat="${cat}" checked>
              <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded ${cfg.badge} group-hover:opacity-80 transition-opacity">${cfg.label}</span>
            </label>
          `).join('')}
        </div>

        <div class="flex-1"></div>

        <input id="console-search" type="text" placeholder="${t('console.filterPlaceholder')}"
          class="bg-[#0d1117] border border-zinc-800 rounded px-2 py-1 text-xs text-zinc-300 w-32 focus:outline-none focus:border-zinc-600 font-mono placeholder:text-zinc-700">

        <button id="btn-pause" class="text-[10px] px-2.5 py-1 rounded font-semibold transition-colors bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200">
          ${t('console.pause')}
        </button>

        <button id="btn-clear" class="text-[10px] px-2.5 py-1 rounded font-semibold bg-zinc-800 text-zinc-500 hover:bg-zinc-700 hover:text-zinc-300 transition-colors">
          ${t('common.clear')}
        </button>
      </div>

      <!-- Terminal -->
      <div class="flex-1 relative min-h-0">
        <div id="console-log"
          class="absolute inset-0 overflow-auto font-mono text-xs rounded-lg border border-zinc-800/80"
          style="background: #0d1117; scrollbar-width: thin; scrollbar-color: #28283d #0d1117;">
          <div class="text-zinc-700 text-center py-8 text-[11px]" id="console-empty">
            ${t('console.waitingForEvents')}
          </div>
        </div>

        <button id="btn-jump-bottom"
          class="hidden absolute bottom-4 right-4 bg-zinc-800/90 text-zinc-400 hover:text-white text-[10px] px-3 py-1.5 rounded-full shadow-lg border border-zinc-700 transition-all hover:bg-zinc-700">
          ${t('console.jumpToBottom')}
        </button>
      </div>
    </div>
  `;

  logEl = document.getElementById('console-log');
  const emptyMsg = document.getElementById('console-empty');
  const btnPause = document.getElementById('btn-pause');
  const btnClear = document.getElementById('btn-clear');
  const btnJump = document.getElementById('btn-jump-bottom');
  const searchInput = document.getElementById('console-search');

  // Track scroll position for auto-scroll
  logEl.addEventListener('scroll', () => {
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    autoScroll = atBottom;
    btnJump.classList.toggle('hidden', atBottom);
  });

  btnJump.addEventListener('click', () => {
    autoScroll = true;
    logEl.scrollTop = logEl.scrollHeight;
    btnJump.classList.add('hidden');
  });

  // Filters
  container.querySelectorAll('.console-filter').forEach(cb => {
    cb.addEventListener('change', () => {
      filters[cb.dataset.cat] = cb.checked;
      rebuildLog(_allEvents);
    });
  });

  // Search
  let searchTimeout;
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
      searchTerm = searchInput.value.trim();
      rebuildLog(_allEvents);
    }, 200);
  });

  // Pause
  paused = false;
  btnPause.addEventListener('click', () => {
    paused = !paused;
    btnPause.textContent = paused ? t('console.resume') : t('console.pause');
    btnPause.classList.toggle('bg-emerald-500/20', paused);
    btnPause.classList.toggle('text-emerald-400', paused);
  });

  // Clear
  btnClear.addEventListener('click', async () => {
    await api.post('/api/console/clear');
    _allEvents = [];
    eventCount = 0;
    logEl.innerHTML = '';
    const counter = document.getElementById('console-count');
    if (counter) counter.textContent = '0';
  });

  // Load history and start SSE
  try {
    const data = await api.get('/api/console/history?limit=500');
    _allEvents = data.events || [];
    if (_allEvents.length > 0 && emptyMsg) emptyMsg.remove();
    rebuildLog(_allEvents);
  } catch {}

  // Override appendEvent to also track in _allEvents
  const origAppend = appendEvent;
  const wrappedSource = new EventSource('/api/console/stream');
  if (eventSource) { eventSource.close(); }
  eventSource = wrappedSource;

  eventSource.onmessage = (e) => {
    if (paused) return;
    try {
      const evt = JSON.parse(e.data);
      if (emptyMsg && emptyMsg.parentNode) emptyMsg.remove();
      // Dedup: skip if we already have this seq from history
      if (_allEvents.length > 0 && evt.seq <= (_allEvents[_allEvents.length - 1]?.seq || 0)) return;
      _allEvents.push(evt);
      if (_allEvents.length > MAX_LINES) _allEvents.splice(0, _allEvents.length - MAX_LINES);
      appendEvent(evt);
    } catch {}
  };

  eventSource.onerror = () => {
    eventSource.close();
    setTimeout(() => {
      if (logEl && logEl.isConnected) connectSSE();
    }, 3000);
  };

  // Cleanup on page nav
  const observer = new MutationObserver(() => {
    if (!logEl || !logEl.isConnected) {
      disconnectSSE();
      observer.disconnect();
      logEl = null;
    }
  });
  observer.observe(container, { childList: true });
}
