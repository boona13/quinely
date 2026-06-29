/** Channels page — Multi-channel messaging configuration, status, and testing */

const t = (key, params) => window.GhostI18n?.t(key, params) ?? key;

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;

  let data;
  try {
    data = await api.get('/api/channels');
  } catch (e) {
    container.innerHTML = `<h1 class="page-header">${t('channels.title')}</h1>
      <div class="stat-card"><p class="text-zinc-500 text-sm">${t('channels.notAvailable')}</p></div>`;
    return;
  }

  const channels = data.channels || [];
  const preferred = data.preferred || '';

  const statusColor = (s) => ({
    connected: 'text-emerald-400', ready: 'text-emerald-400',
    'webhook-only': 'text-blue-400', error: 'text-red-400',
    'not configured': 'text-zinc-600',
  }[s] || 'text-zinc-500');

  const statusDot = (s) => {
    const c = { connected: 'bg-emerald-400', ready: 'bg-emerald-400',
      'webhook-only': 'bg-blue-400', error: 'bg-red-400' }[s] || 'bg-zinc-600';
    return `<span class="inline-block w-2 h-2 rounded-full ${c}"></span>`;
  };

  const capBadges = (ch) => {
    const caps = [];
    if (ch.supports_inbound) caps.push('inbound');
    if (ch.supports_media) caps.push('media');
    if (ch.supports_threads) caps.push('threads');
    if (ch.supports_groups) caps.push('groups');
    return caps.map(c =>
      `<span class="text-[9px] px-1 py-0.5 rounded bg-surface-600/50 text-zinc-500">${c}</span>`
    ).join(' ');
  };

  const configured = channels.filter(c => c.configured && c.enabled !== false);
  const notConfigured = channels.filter(c => !c.configured || c.enabled === false);

  container.innerHTML = `
    <h1 class="page-header">${t('channels.title')}</h1>
    <p class="page-desc">${t('channels.subtitle')}</p>

    <div class="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
      <div class="stat-card">
        <div class="text-2xl font-bold text-white">${configured.length}</div>
        <div class="text-xs text-zinc-500">${t('channels.configured')}</div>
      </div>
      <div class="stat-card">
        <div class="text-2xl font-bold text-white">${channels.length}</div>
        <div class="text-xs text-zinc-500">${t('channels.available')}</div>
      </div>
      <div class="stat-card">
        <div class="text-2xl font-bold text-emerald-400">${preferred || 'none'}</div>
        <div class="text-xs text-zinc-500">${t('channels.preferredChannel')}</div>
      </div>
    </div>

    <div class="mb-6">
      <h3 class="text-sm font-semibold text-white mb-3">${t('channels.quickSend')}</h3>
      <div class="stat-card">
        <div class="flex gap-2">
          <input id="ch-quick-msg" type="text" placeholder="${t('channels.typePlaceholder')}"
            class="flex-1 bg-surface-700 border border-surface-600 rounded px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500" />
          <select id="ch-quick-channel" class="bg-surface-700 border border-surface-600 rounded px-2 py-2 text-sm text-zinc-300">
            <option value="">${t('channels.autoPreferred')}</option>
            ${configured.map(c => `<option value="${c.id}">${c.emoji} ${c.label}</option>`).join('')}
          </select>
          <button id="ch-quick-send" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm rounded font-medium">${t('common.send')}</button>
        </div>
        <div id="ch-quick-result" class="text-xs text-zinc-500 mt-2 hidden"></div>
      </div>
    </div>

    ${configured.length > 0 ? `
    <h3 class="text-sm font-semibold text-white mb-3">${t('channels.configuredChannels')}</h3>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
      ${configured.map(ch => `
        <div class="stat-card channel-card" data-channel="${ch.id}">
          <div class="flex items-center gap-3 mb-3">
            <span class="text-2xl">${ch.emoji}</span>
            <div class="flex-1">
              <div class="flex items-center gap-2">
                <span class="text-sm font-semibold text-white">${u.escapeHtml(ch.label)}</span>
                ${ch.id === preferred ? `<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400">${t('channels.preferredBadge')}</span>` : ''}
              </div>
              <div class="flex items-center gap-1.5 mt-0.5">
                ${statusDot(ch.status)}
                <span class="text-[11px] ${statusColor(ch.status)}">${ch.status}</span>
              </div>
            </div>
            <div class="flex gap-1">
              <button class="btn-test text-[10px] px-2 py-1 rounded bg-blue-500/20 text-blue-400 hover:bg-blue-500/30" data-id="${ch.id}">${t('common.test')}</button>
              <button class="btn-set-preferred text-[10px] px-2 py-1 rounded bg-zinc-700 text-zinc-400 hover:bg-zinc-600" data-id="${ch.id}">${t('channels.setDefault')}</button>
            </div>
          </div>
          <div class="flex flex-wrap gap-1 mb-2">${capBadges(ch)}</div>
          <div class="flex gap-2">
            <button class="btn-configure text-[10px] px-2 py-1 rounded bg-surface-600 text-zinc-400 hover:bg-surface-500" data-id="${ch.id}">${t('common.configure')}</button>
            <button class="btn-disable text-[10px] px-2 py-1 rounded bg-red-500/10 text-red-400 hover:bg-red-500/20" data-id="${ch.id}">${t('common.disable')}</button>
          </div>
        </div>
      `).join('')}
    </div>
    ` : ''}

    ${notConfigured.length > 0 ? `
    <h3 class="text-sm font-semibold text-zinc-400 mb-3">${t('channels.available')} ${t('channels.title')}</h3>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 mb-6">
      ${notConfigured.map(ch => `
        <div class="stat-card opacity-70 hover:opacity-100 transition-opacity">
          <div class="flex items-center gap-3">
            <span class="text-xl">${ch.emoji}</span>
            <div class="flex-1">
              <span class="text-sm font-medium text-zinc-300">${u.escapeHtml(ch.label)}</span>
              <div class="flex flex-wrap gap-1 mt-1">${capBadges(ch)}</div>
            </div>
            <button class="btn-configure text-[10px] px-2 py-1 rounded bg-surface-600 text-zinc-400 hover:bg-surface-500" data-id="${ch.id}">${t('channels.setup')}</button>
          </div>
          ${ch.docs_url ? `<a href="${ch.docs_url}" target="_blank" class="text-[10px] text-zinc-600 hover:text-zinc-400 mt-2 block">${t('channels.docsLink')}</a>` : ''}
        </div>
      `).join('')}
    </div>
    ` : ''}

    <h3 class="text-sm font-semibold text-zinc-400 mb-3">${t('channels.recentInbound')}</h3>
    <div id="ch-inbound-log" class="stat-card">
      <div class="text-xs text-zinc-600 py-4 text-center animate-pulse">${t('common.loading')}</div>
    </div>
  `;

  // Quick send
  const sendBtn = container.querySelector('#ch-quick-send');
  const msgInput = container.querySelector('#ch-quick-msg');
  const channelSelect = container.querySelector('#ch-quick-channel');
  const resultDiv = container.querySelector('#ch-quick-result');

  sendBtn?.addEventListener('click', async () => {
    const message = msgInput.value.trim();
    if (!message) return;
    sendBtn.disabled = true;
    sendBtn.textContent = t('channels.sending');
    try {
      const res = await api.post('/api/channels/send', {
        message, channel: channelSelect.value || undefined,
      });
      resultDiv.classList.remove('hidden');
      if (res.ok) {
        resultDiv.textContent = t('channels.sentVia', { channel: res.channel }) + (res.message_id ? ` (${res.message_id})` : '');
        resultDiv.className = 'text-xs text-emerald-400 mt-2';
        msgInput.value = '';
      } else {
        resultDiv.textContent = t('common.failedWithError', {error: res.error});
        resultDiv.className = 'text-xs text-red-400 mt-2';
      }
    } catch (e) {
      resultDiv.classList.remove('hidden');
      resultDiv.textContent = t('common.errorPrefix', {error: e.message});
      resultDiv.className = 'text-xs text-red-400 mt-2';
    }
    sendBtn.disabled = false;
    sendBtn.textContent = t('common.send');
  });

  msgInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendBtn?.click();
  });

  // Test buttons
  container.querySelectorAll('.btn-test').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      btn.textContent = t('common.testing');
      try {
        const res = await api.post(`/api/channels/${id}/test`, {});
        btn.textContent = res.ok ? t('common.done') : t('common.error');
        setTimeout(() => { btn.textContent = t('common.test'); }, 2000);
      } catch (e) {
        btn.textContent = t('common.error');
        setTimeout(() => { btn.textContent = t('common.test'); }, 2000);
      }
    });
  });

  // Set preferred
  container.querySelectorAll('.btn-set-preferred').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      const origText = btn.textContent;
      btn.disabled = true;
      btn.textContent = t('channels.setting');
      btn.classList.add('opacity-60');
      try {
        await api.post('/api/channels/preferred', { channel: id });
        btn.textContent = t('common.done');
        btn.classList.remove('bg-zinc-700', 'text-zinc-400');
        btn.classList.add('bg-emerald-500/20', 'text-emerald-400');
        setTimeout(() => render(container), 800);
      } catch (e) {
        btn.textContent = t('common.error');
        btn.classList.remove('bg-zinc-700', 'text-zinc-400');
        btn.classList.add('bg-red-500/20', 'text-red-400');
        setTimeout(() => {
          btn.textContent = origText;
          btn.disabled = false;
          btn.classList.remove('opacity-60', 'bg-red-500/20', 'text-red-400');
          btn.classList.add('bg-zinc-700', 'text-zinc-400');
        }, 2000);
      }
    });
  });

  // Disable buttons
  container.querySelectorAll('.btn-disable').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      btn.disabled = true;
      btn.textContent = t('channels.disabling');
      btn.classList.add('opacity-60');
      try {
        await api.post(`/api/channels/${id}/disable`);
        btn.textContent = t('common.disabled');
        setTimeout(() => render(container), 600);
      } catch (e) {
        btn.textContent = t('common.error');
        setTimeout(() => {
          btn.textContent = t('common.disable');
          btn.disabled = false;
          btn.classList.remove('opacity-60');
        }, 2000);
      }
    });
  });

  // Configure buttons — open modal (WhatsApp gets special QR modal)
  container.querySelectorAll('.btn-configure').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      if (id === 'whatsapp') {
        showWhatsAppModal(container);
        return;
      }
      if (id === 'telegram') {
        showTelegramModal(container);
        return;
      }
      if (id === 'discord') {
        showDiscordModal(container);
        return;
      }
      try {
        const schemaData = await api.get(`/api/channels/${id}/schema`);
        showConfigModal(id, schemaData.schema, schemaData.current, container);
      } catch (e) {
        alert(t('channels.failedLoadSchema', {error: e.message}));
      }
    });
  });

  // Load inbound log
  try {
    const logData = await api.get('/api/channels/inbound/log?limit=20');
    const entries = logData.entries || [];
    const logContainer = container.querySelector('#ch-inbound-log');
    if (entries.length === 0) {
      logContainer.innerHTML = `<div class="text-xs text-zinc-600 py-4 text-center">${t('channels.noInbound')}</div>`;
    } else {
      logContainer.innerHTML = entries.map(e => `
        <div class="flex items-center gap-2 py-2 border-b border-surface-600/30 last:border-0">
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-600 text-zinc-400">${u.escapeHtml(e.channel)}</span>
          <span class="text-[11px] text-zinc-400 font-medium">${u.escapeHtml(e.sender_name)}</span>
          <span class="text-[11px] text-zinc-500 flex-1 truncate">${u.escapeHtml(e.text)}</span>
          <span class="text-[10px] text-zinc-600">${new Date(e.timestamp * 1000).toLocaleTimeString()}</span>
        </div>
      `).join('');
    }
  } catch (_) {
    const logContainer = container.querySelector('#ch-inbound-log');
    if (logContainer) logContainer.innerHTML = `<div class="text-xs text-zinc-600 py-4 text-center">${t('channels.couldNotLoadInbound')}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════
//  WhatsApp QR Code Modal
// ═══════════════════════════════════════════════════════════════

function showWhatsAppModal(pageContainer) {
  const { GhostAPI: api } = window;

  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4';
  overlay.innerHTML = `
    <div class="bg-surface-800 rounded-xl border border-surface-600 p-6 w-full max-w-md shadow-2xl">
      <div class="flex items-center gap-3 mb-5">
        <span class="text-3xl">📱</span>
        <div>
          <h3 class="text-sm font-semibold text-white">${t('channels.whatsappSetup')}</h3>
          <p class="text-[11px] text-zinc-500">${t('channels.linkWhatsapp')}</p>
        </div>
      </div>

      <!-- Mode selector -->
      <div id="wa-mode-select" class="mb-5">
        <label class="block text-[11px] text-zinc-400 mb-2">${t('channels.connectionMode')}</label>
        <div class="grid grid-cols-2 gap-2">
          <button id="wa-mode-web" class="wa-mode-btn active px-3 py-2.5 rounded-lg border-2 border-emerald-500/50 bg-emerald-500/10 text-left transition-all">
            <div class="text-xs font-semibold text-white">${t('channels.qrCodeScan')}</div>
            <div class="text-[10px] text-zinc-400 mt-0.5">${t('channels.personalWhatsapp')}</div>
          </button>
          <button id="wa-mode-biz" class="wa-mode-btn px-3 py-2.5 rounded-lg border-2 border-surface-600 bg-surface-700 text-left transition-all hover:border-zinc-500">
            <div class="text-xs font-semibold text-zinc-300">${t('channels.businessApi')}</div>
            <div class="text-[10px] text-zinc-500 mt-0.5">${t('channels.cloudApiWebhooks')}</div>
          </button>
        </div>
      </div>

      <!-- Web mode: QR code area -->
      <div id="wa-web-panel">
        <div id="wa-qr-area" class="flex flex-col items-center justify-center py-6">
          <div id="wa-qr-placeholder" class="text-center">
            <div class="w-16 h-16 mx-auto mb-3 rounded-xl bg-surface-700 flex items-center justify-center">
              <svg class="w-8 h-8 text-zinc-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                  d="M12 4v1m6 11h2m-6 0h-2v4m0-11v3m0 0h.01M12 12h4.01M16 20h4M4 12h4m12 0h.01M5 8h2a1 1 0 001-1V5a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1zm12 0h2a1 1 0 001-1V5a1 1 0 00-1-1h-2a1 1 0 00-1 1v2a1 1 0 001 1zM5 20h2a1 1 0 001-1v-2a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1z"/>
              </svg>
            </div>
            <p class="text-xs text-zinc-400 mb-4">${t('channels.clickToGenerateQr')}</p>
            <button id="wa-start-link" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm rounded-lg font-medium transition-colors">
              ${t('channels.generateQr')}
            </button>
          </div>
          <div id="wa-qr-loading" class="text-center hidden">
            <div class="w-48 h-48 mx-auto mb-3 rounded-xl bg-surface-700 flex items-center justify-center animate-pulse">
              <svg class="w-8 h-8 text-zinc-600 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
              </svg>
            </div>
            <p class="text-xs text-zinc-400">${t('channels.connectingWa')}</p>
          </div>
          <div id="wa-qr-display" class="text-center hidden">
            <div class="bg-white p-3 rounded-xl inline-block mb-3 shadow-lg">
              <img id="wa-qr-img" src="" alt="WhatsApp QR Code" class="w-52 h-52" />
            </div>
            <p class="text-xs text-zinc-300 font-medium">${t('channels.scanWithWa')}</p>
            <p class="text-[10px] text-zinc-500 mt-1">${t('channels.scanInstructions')}</p>
            <div id="wa-qr-timer" class="text-[10px] text-zinc-600 mt-2"></div>
          </div>
          <div id="wa-connected" class="text-center hidden">
            <div class="w-16 h-16 mx-auto mb-3 rounded-full bg-emerald-500/20 flex items-center justify-center">
              <svg class="w-8 h-8 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
              </svg>
            </div>
            <p class="text-sm font-semibold text-emerald-400">${t('channels.waLinked')}</p>
            <p class="text-[11px] text-zinc-400 mt-1">${t('channels.waConnected')}</p>
          </div>
          <div id="wa-error" class="text-center hidden">
            <div class="w-16 h-16 mx-auto mb-3 rounded-full bg-red-500/20 flex items-center justify-center">
              <svg class="w-8 h-8 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
              </svg>
            </div>
            <p id="wa-error-title" class="text-sm font-semibold text-red-400">${t('channels.connectionFailed')}</p>
            <p id="wa-error-msg" class="text-[11px] text-zinc-400 mt-1"></p>
            <pre id="wa-error-hint" class="hidden text-left text-[11px] text-amber-300/90 bg-surface-700 rounded-lg px-4 py-3 mt-3 mx-auto max-w-xs whitespace-pre-wrap font-mono"></pre>
            <button id="wa-retry" class="mt-3 px-3 py-1.5 bg-surface-600 text-zinc-300 text-xs rounded hover:bg-surface-500">${t('common.retry')}</button>
          </div>
        </div>

        
      </div>

      <!-- Business mode panel (hidden by default) -->
      <div id="wa-biz-panel" class="hidden space-y-3">
        <div>
          <label class="block text-[11px] text-zinc-400 mb-1">${t('channels.accessToken')}</label>
          <input id="wa-biz-token" type="password" placeholder="${t('channels.accessTokenDesc')}"
            class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500" />
        </div>
        <div>
          <label class="block text-[11px] text-zinc-400 mb-1">${t('channels.phoneNumberId')}</label>
          <input id="wa-biz-phone" type="text" placeholder="${t('channels.phoneNumberIdDesc')}"
            class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500" />
        </div>
        <div>
          <label class="block text-[11px] text-zinc-400 mb-1">${t('channels.defaultRecipient')}</label>
          <input id="wa-biz-recipient" type="text" placeholder="+1234567890"
            class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500" />
        </div>
        <div>
          <label class="block text-[11px] text-zinc-400 mb-1">${t('channels.verifyToken')}</label>
          <input id="wa-biz-verify" type="password" placeholder="ghost_whatsapp_verify" value="ghost_whatsapp_verify"
            class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500" />
        </div>
      </div>

      <!-- Footer -->
      <div class="flex justify-between items-center mt-5 pt-4 border-t border-surface-600/50">
        <button id="wa-logout" class="text-[10px] px-2 py-1 rounded bg-red-500/10 text-red-400 hover:bg-red-500/20 hidden">${t('channels.unlinkWhatsapp')}</button>
        <div class="flex gap-2 ml-auto">
          <button id="wa-cancel" class="px-3 py-1.5 rounded bg-surface-600 text-zinc-400 text-sm hover:bg-surface-500">${t('common.cancel')}</button>
          <button id="wa-save-biz" class="px-3 py-1.5 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-500 font-medium hidden">${t('common.save')}</button>
        </div>
      </div>
      <div id="wa-result" class="text-xs mt-2 hidden"></div>
    </div>
  `;

  document.body.appendChild(overlay);

  const $ = (sel) => overlay.querySelector(sel);
  let currentMode = 'web';
  let pollTimer = null;

  const switchPanel = (mode) => {
    currentMode = mode;
    if (mode === 'web') {
      $('#wa-mode-web').classList.add('border-emerald-500/50', 'bg-emerald-500/10');
      $('#wa-mode-web').classList.remove('border-surface-600', 'bg-surface-700');
      $('#wa-mode-biz').classList.remove('border-emerald-500/50', 'bg-emerald-500/10');
      $('#wa-mode-biz').classList.add('border-surface-600', 'bg-surface-700');
      $('#wa-web-panel').classList.remove('hidden');
      $('#wa-biz-panel').classList.add('hidden');
      $('#wa-save-biz').classList.add('hidden');
    } else {
      $('#wa-mode-biz').classList.add('border-emerald-500/50', 'bg-emerald-500/10');
      $('#wa-mode-biz').classList.remove('border-surface-600', 'bg-surface-700');
      $('#wa-mode-web').classList.remove('border-emerald-500/50', 'bg-emerald-500/10');
      $('#wa-mode-web').classList.add('border-surface-600', 'bg-surface-700');
      $('#wa-web-panel').classList.add('hidden');
      $('#wa-biz-panel').classList.remove('hidden');
      $('#wa-save-biz').classList.remove('hidden');
    }
  };

  $('#wa-mode-web').addEventListener('click', () => switchPanel('web'));
  $('#wa-mode-biz').addEventListener('click', () => switchPanel('business'));

  const showView = (viewId) => {
    ['wa-qr-placeholder', 'wa-qr-loading', 'wa-qr-display', 'wa-connected', 'wa-error'].forEach(id => {
      $(`#${id}`).classList.toggle('hidden', id !== viewId);
    });
  };

  const stopPolling = () => {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };

  const cleanup = () => {
    stopPolling();
    overlay.remove();
  };

  overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(); });
  $('#wa-cancel').addEventListener('click', cleanup);

  // Start QR linking
  const startLink = async () => {
    showView('wa-qr-loading');
    try {
      const res = await api.post('/api/channels/whatsapp/qr/start', { mode: 'web' });
      if (res.ok && res.status === 'already_connected') {
        showView('wa-connected');
        $('#wa-logout').classList.remove('hidden');
        return;
      }
      if (res.ok && res.qr_data_url) {
        $('#wa-qr-img').src = res.qr_data_url;
        showView('wa-qr-display');
        startPolling();
        return;
      }
      if (!res.ok) {
        if (res.error_type === 'missing_dependency') {
          $('#wa-error-title').textContent = t('channels.missingDependency');
          $('#wa-error-msg').textContent = t('channels.missingDependencyDesc');
          const hint = $('#wa-error-hint');
          hint.textContent = res.error || 'Run: pip install neonize';
          hint.classList.remove('hidden');
          $('#wa-retry').classList.add('hidden');
        } else {
          $('#wa-error-msg').textContent = res.error || t('channels.qrFailed');
        }
        showView('wa-error');
        return;
      }
      showView('wa-qr-loading');
      startPolling();
    } catch (e) {
      $('#wa-error-msg').textContent = e.message;
      showView('wa-error');
    }
  };

  const startPolling = () => {
    stopPolling();
    let elapsed = 0;
    pollTimer = setInterval(async () => {
      elapsed += 2;
      const timerEl = $('#wa-qr-timer');
      if (timerEl) timerEl.textContent = `${t('channels.waitingForScan')} (${elapsed}s)`;
      try {
        const status = await api.get('/api/channels/whatsapp/qr/status');
        if (status.connected) {
          stopPolling();
          showView('wa-connected');
          $('#wa-logout').classList.remove('hidden');

          await api.post('/api/channels/whatsapp/configure', {
            mode: 'web', enabled: true,
          });
          setTimeout(() => { cleanup(); render(pageContainer); }, 2000);
          return;
        }
        if (status.status === 'error') {
          stopPolling();
          $('#wa-error-msg').textContent = status.error || t('channels.connectionError');
          showView('wa-error');
          return;
        }
        if (status.qr_data_url) {
          $('#wa-qr-img').src = status.qr_data_url;
          showView('wa-qr-display');
        }
      } catch (_) {}
    }, 2000);
  };

  $('#wa-start-link').addEventListener('click', startLink);
  $('#wa-retry')?.addEventListener('click', startLink);

  // Logout
  $('#wa-logout').addEventListener('click', async () => {
    try {
      await api.post('/api/channels/whatsapp/logout');
      showView('wa-qr-placeholder');
      $('#wa-logout').classList.add('hidden');
    } catch (e) {
      const r = $('#wa-result');
      r.classList.remove('hidden');
      r.textContent = t('channels.logoutError', {error: e.message});
      r.className = 'text-xs text-red-400 mt-2';
    }
  });

  // Business mode save
  $('#wa-save-biz').addEventListener('click', async () => {
    const token = $('#wa-biz-token').value.trim();
    const phone = $('#wa-biz-phone').value.trim();
    const recipient = $('#wa-biz-recipient').value.trim();
    const verify = $('#wa-biz-verify').value.trim();
    if (!token || !phone) {
      const r = $('#wa-result');
      r.classList.remove('hidden');
      r.textContent = t('channels.requiredFields');
      r.className = 'text-xs text-red-400 mt-2';
      return;
    }
    try {
      const res = await api.post('/api/channels/whatsapp/configure', {
        mode: 'business', access_token: token, phone_number_id: phone,
        default_recipient: recipient, verify_token: verify || 'ghost_whatsapp_verify',
        enabled: true,
      });
      const r = $('#wa-result');
      r.classList.remove('hidden');
      if (res.ok) {
        r.textContent = t('channels.waBusinessConfigured');
        r.className = 'text-xs text-emerald-400 mt-2';
        setTimeout(() => { cleanup(); render(pageContainer); }, 1000);
      } else {
        r.textContent = res.message || t('channels.configIssue');
        r.className = 'text-xs text-amber-400 mt-2';
      }
    } catch (e) {
      const r = $('#wa-result');
      r.classList.remove('hidden');
      r.textContent = t('common.errorPrefix', {error: e.message});
      r.className = 'text-xs text-red-400 mt-2';
    }
  });

  // Check initial status
  (async () => {
    try {
      const status = await api.get('/api/channels/whatsapp/qr/status');
      if (status.connected) {
        showView('wa-connected');
        $('#wa-logout').classList.remove('hidden');
      } else if (status.status === 'linking' && status.qr_data_url) {
        $('#wa-qr-img').src = status.qr_data_url;
        showView('wa-qr-display');
        startPolling();
      }
    } catch (_) {}
  })();
}


// ═══════════════════════════════════════════════════════════════
//  Telegram Setup Wizard
// ═══════════════════════════════════════════════════════════════

function showTelegramModal(pageContainer) {
  const { GhostAPI: api, GhostUtils: u } = window;

  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4';
  overlay.innerHTML = `
    <div class="bg-surface-800 rounded-xl border border-surface-600 p-6 w-full max-w-md shadow-2xl">
      <div class="flex items-center gap-3 mb-5">
        <span class="text-3xl">\u{1f4e8}</span>
        <div>
          <h3 class="text-sm font-semibold text-white">${t('channels.telegramSetup')}</h3>
          <p class="text-[11px] text-zinc-500">${t('channels.linkTelegram')}</p>
        </div>
      </div>

      <!-- Step indicator -->
      <div id="tg-steps" class="flex items-center gap-2 mb-5">
        <div class="tg-step-dot w-2 h-2 rounded-full bg-emerald-400"></div>
        <div class="tg-step-bar flex-1 h-0.5 bg-surface-600 rounded"><div id="tg-bar-1" class="h-full rounded bg-surface-600 transition-all duration-500" style="width:0%"></div></div>
        <div class="tg-step-dot w-2 h-2 rounded-full bg-surface-600"></div>
        <div class="tg-step-bar flex-1 h-0.5 bg-surface-600 rounded"><div id="tg-bar-2" class="h-full rounded bg-surface-600 transition-all duration-500" style="width:0%"></div></div>
        <div class="tg-step-dot w-2 h-2 rounded-full bg-surface-600"></div>
      </div>

      <!-- Step 1: Bot Token -->
      <div id="tg-step-1">
        <div class="text-[10px] text-zinc-600 mb-3">${t('channels.telegramStep', {n: '1'})}</div>
        <label class="block text-[11px] text-zinc-400 mb-1.5">${t('channels.enterBotToken')}</label>
        <input id="tg-token" type="password" placeholder="${t('channels.botTokenPlaceholder')}"
          class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2.5 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500 font-mono" />
        <div id="tg-token-status" class="text-[11px] mt-2 hidden"></div>
        <p class="text-[10px] text-zinc-600 mt-3">${t('channels.createBotHint')}</p>
        <div class="flex justify-end gap-2 mt-4">
          <button id="tg-cancel-1" class="px-3 py-1.5 rounded bg-surface-600 text-zinc-400 text-sm hover:bg-surface-500">${t('common.cancel')}</button>
          <button id="tg-next-1" class="px-4 py-1.5 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-500 font-medium opacity-50 cursor-not-allowed" disabled>${t('common.next')}</button>
        </div>
      </div>

      <!-- Step 2: Send message to bot -->
      <div id="tg-step-2" class="hidden">
        <div class="text-[10px] text-zinc-600 mb-3">${t('channels.telegramStep', {n: '2'})}</div>
        <div class="flex flex-col items-center py-4">
          <div class="w-14 h-14 rounded-full bg-blue-500/15 flex items-center justify-center mb-4">
            <svg class="w-7 h-7 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/>
            </svg>
          </div>
          <p class="text-xs text-zinc-300 text-center font-medium">${t('channels.sendStartToBot')}</p>
          <p id="tg-bot-handle" class="text-sm text-emerald-400 font-semibold mt-1">@bot</p>
          <div id="tg-detect-status" class="flex items-center gap-2 mt-4">
            <div class="w-3 h-3 rounded-full bg-amber-400/80 animate-pulse"></div>
            <span class="text-[11px] text-zinc-400">${t('channels.waitingForMessage')}</span>
          </div>
          <div id="tg-detect-timer" class="text-[10px] text-zinc-600 mt-1"></div>
        </div>
        <div class="flex justify-end gap-2 mt-2">
          <button id="tg-back-2" class="px-3 py-1.5 rounded bg-surface-600 text-zinc-400 text-sm hover:bg-surface-500">${t('common.back')}</button>
          <button id="tg-cancel-2" class="px-3 py-1.5 rounded bg-surface-600 text-zinc-400 text-sm hover:bg-surface-500">${t('common.cancel')}</button>
        </div>
      </div>

      <!-- Step 3: Connected -->
      <div id="tg-step-3" class="hidden">
        <div class="flex flex-col items-center py-6">
          <div class="w-16 h-16 rounded-full bg-emerald-500/20 flex items-center justify-center mb-3">
            <svg class="w-8 h-8 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
            </svg>
          </div>
          <p class="text-sm font-semibold text-emerald-400">${t('channels.telegramConnected')}</p>
          <p class="text-[11px] text-zinc-400 mt-1">${t('channels.telegramConnectedDesc')}</p>
          <div id="tg-test-result" class="text-[10px] text-zinc-500 mt-3"></div>
        </div>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  const $ = (sel) => overlay.querySelector(sel);
  let pollTimer = null;
  let validatedToken = '';
  let botUsername = '';

  const cleanup = () => {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    overlay.remove();
  };

  overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(); });
  $('#tg-cancel-1').addEventListener('click', cleanup);
  $('#tg-cancel-2').addEventListener('click', cleanup);

  const showStep = (step) => {
    $('#tg-step-1').classList.toggle('hidden', step !== 1);
    $('#tg-step-2').classList.toggle('hidden', step !== 2);
    $('#tg-step-3').classList.toggle('hidden', step !== 3);
    const dots = overlay.querySelectorAll('.tg-step-dot');
    dots.forEach((d, i) => {
      d.classList.toggle('bg-emerald-400', i < step);
      d.classList.toggle('bg-surface-600', i >= step);
    });
    if (step >= 2) { $('#tg-bar-1').style.width = '100%'; $('#tg-bar-1').classList.add('bg-emerald-400'); }
    if (step >= 3) { $('#tg-bar-2').style.width = '100%'; $('#tg-bar-2').classList.add('bg-emerald-400'); }
  };

  // Step 1: Token validation with debounce
  const tokenInput = $('#tg-token');
  const tokenStatus = $('#tg-token-status');
  const nextBtn = $('#tg-next-1');
  let validateTimeout = null;

  const validateToken = async (token) => {
    if (!token || !token.match(/^\d+:.+$/)) {
      tokenStatus.classList.add('hidden');
      nextBtn.disabled = true;
      nextBtn.classList.add('opacity-50', 'cursor-not-allowed');
      return;
    }
    tokenStatus.classList.remove('hidden');
    tokenStatus.textContent = t('channels.validatingToken');
    tokenStatus.className = 'text-[11px] mt-2 text-zinc-400 animate-pulse';

    try {
      const res = await api.post('/api/channels/telegram/bot-info', { bot_token: token });
      if (res.ok) {
        validatedToken = token;
        botUsername = res.username;
        tokenStatus.textContent = t('channels.botVerified', { username: res.username });
        tokenStatus.className = 'text-[11px] mt-2 text-emerald-400';
        nextBtn.disabled = false;
        nextBtn.classList.remove('opacity-50', 'cursor-not-allowed');
      } else {
        tokenStatus.textContent = t('channels.invalidBotToken');
        tokenStatus.className = 'text-[11px] mt-2 text-red-400';
        nextBtn.disabled = true;
        nextBtn.classList.add('opacity-50', 'cursor-not-allowed');
      }
    } catch (_) {
      tokenStatus.textContent = t('channels.invalidBotToken');
      tokenStatus.className = 'text-[11px] mt-2 text-red-400';
      nextBtn.disabled = true;
      nextBtn.classList.add('opacity-50', 'cursor-not-allowed');
    }
  };

  tokenInput.addEventListener('input', () => {
    if (validateTimeout) clearTimeout(validateTimeout);
    validateTimeout = setTimeout(() => validateToken(tokenInput.value.trim()), 600);
  });

  tokenInput.addEventListener('paste', () => {
    setTimeout(() => validateToken(tokenInput.value.trim()), 100);
  });

  // Step 1 → Step 2
  nextBtn.addEventListener('click', async () => {
    if (!validatedToken) return;

    try {
      await api.post('/api/channels/telegram/configure', {
        bot_token: validatedToken,
        enabled: true,
      });
    } catch (_) {}

    $('#tg-bot-handle').textContent = `@${botUsername}`;
    showStep(2);
    startDetection();
  });

  // Step 2 → back to Step 1
  $('#tg-back-2').addEventListener('click', () => {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    showStep(1);
  });

  // Step 2: Poll for chat detection
  const startDetection = () => {
    if (pollTimer) clearInterval(pollTimer);
    let elapsed = 0;
    const timerEl = $('#tg-detect-timer');
    const statusEl = $('#tg-detect-status');

    pollTimer = setInterval(async () => {
      elapsed += 3;
      if (timerEl) timerEl.textContent = `${elapsed}s`;

      try {
        const res = await api.post('/api/channels/telegram/detect-chat', {
          bot_token: validatedToken,
        });
        if (res.ok) {
          clearInterval(pollTimer);
          pollTimer = null;

          statusEl.innerHTML = `
            <div class="w-3 h-3 rounded-full bg-emerald-400"></div>
            <span class="text-[11px] text-emerald-400">${t('channels.chatDetected', { name: res.sender_name || res.chat_id })}</span>
          `;
          if (timerEl && res.added_to_allowlist) {
            timerEl.innerHTML = `<span class="text-emerald-400/70">\u{1f512} ${t('channels.senderAllowlisted')}</span>`;
          }

          setTimeout(() => {
            showStep(3);
            sendTestMessage();
          }, 1200);
        }
      } catch (_) {}
    }, 3000);
  };

  const sendTestMessage = async () => {
    const resultEl = $('#tg-test-result');
    try {
      const res = await api.post('/api/channels/telegram/test', {
        message: '\u{2705} Quinely is connected to Telegram! Setup complete.',
      });
      if (res.ok) {
        resultEl.textContent = 'Test message sent to your Telegram!';
        resultEl.className = 'text-[10px] text-emerald-400/70 mt-3';
      }
    } catch (_) {}
    setTimeout(() => { cleanup(); render(pageContainer); }, 2500);
  };
}


// ═══════════════════════════════════════════════════════════════
//  Discord Setup Wizard
// ═══════════════════════════════════════════════════════════════

function showDiscordModal(pageContainer) {
  const { GhostAPI: api, GhostUtils: u } = window;

  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4';
  overlay.innerHTML = `
    <div class="bg-surface-800 rounded-xl border border-surface-600 p-6 w-full max-w-md shadow-2xl">
      <div class="flex items-center gap-3 mb-5">
        <span class="text-3xl">\u{1f3ae}</span>
        <div>
          <h3 class="text-sm font-semibold text-white">${t('channels.discordSetup')}</h3>
          <p class="text-[11px] text-zinc-500">${t('channels.linkDiscord')}</p>
        </div>
      </div>

      <!-- Mode selector -->
      <div id="dc-mode-select">
        <div class="grid grid-cols-2 gap-2 mb-5">
          <button id="dc-mode-bot" class="dc-mode-btn px-3 py-2.5 rounded-lg border-2 border-indigo-500/50 bg-indigo-500/10 text-left transition-all">
            <div class="text-xs font-semibold text-white">${t('channels.discordModeBot')}</div>
            <div class="text-[10px] text-zinc-400 mt-0.5">${t('channels.discordModeBotDesc')}</div>
          </button>
          <button id="dc-mode-webhook" class="dc-mode-btn px-3 py-2.5 rounded-lg border-2 border-surface-600 bg-surface-700 text-left transition-all hover:border-zinc-500">
            <div class="text-xs font-semibold text-zinc-300">${t('channels.discordModeWebhook')}</div>
            <div class="text-[10px] text-zinc-500 mt-0.5">${t('channels.discordModeWebhookDesc')}</div>
          </button>
        </div>
      </div>

      <!-- ════ Webhook flow ════ -->
      <div id="dc-webhook-panel" class="hidden">
        <label class="block text-[11px] text-zinc-400 mb-1.5">${t('channels.enterWebhookUrl')}</label>
        <input id="dc-webhook-url" type="text" placeholder="${t('channels.webhookPlaceholder')}"
          class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2.5 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500 font-mono" />
        <div id="dc-webhook-status" class="text-[11px] mt-2 hidden"></div>
        <div class="flex justify-end gap-2 mt-4">
          <button id="dc-wh-back" class="px-3 py-1.5 rounded bg-surface-600 text-zinc-400 text-sm hover:bg-surface-500">${t('common.back')}</button>
          <button id="dc-wh-save" class="px-4 py-1.5 rounded bg-indigo-600 text-white text-sm hover:bg-indigo-500 font-medium opacity-50 cursor-not-allowed" disabled>${t('common.save')}</button>
        </div>
      </div>

      <!-- ════ Bot flow ════ -->

      <!-- Bot Step 1: Token -->
      <div id="dc-bot-step-1">
        <div id="dc-bot-steps" class="flex items-center gap-2 mb-4">
          <div class="dc-step-dot w-2 h-2 rounded-full bg-indigo-400"></div>
          <div class="dc-step-bar flex-1 h-0.5 bg-surface-600 rounded"><div id="dc-bar-1" class="h-full rounded bg-surface-600 transition-all duration-500" style="width:0%"></div></div>
          <div class="dc-step-dot w-2 h-2 rounded-full bg-surface-600"></div>
          <div class="dc-step-bar flex-1 h-0.5 bg-surface-600 rounded"><div id="dc-bar-2" class="h-full rounded bg-surface-600 transition-all duration-500" style="width:0%"></div></div>
          <div class="dc-step-dot w-2 h-2 rounded-full bg-surface-600"></div>
          <div class="dc-step-bar flex-1 h-0.5 bg-surface-600 rounded"><div id="dc-bar-3" class="h-full rounded bg-surface-600 transition-all duration-500" style="width:0%"></div></div>
          <div class="dc-step-dot w-2 h-2 rounded-full bg-surface-600"></div>
        </div>
        <div class="text-[10px] text-zinc-600 mb-3">${t('channels.discordStep', {n: '1', total: '4'})}</div>
        <label class="block text-[11px] text-zinc-400 mb-1.5">${t('channels.enterBotTokenDiscord')}</label>
        <input id="dc-token" type="password" placeholder="${t('channels.botTokenDiscordPlaceholder')}"
          class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2.5 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500 font-mono" />
        <div id="dc-token-status" class="text-[11px] mt-2 hidden"></div>
        <p class="text-[10px] text-zinc-600 mt-3">${t('channels.createBotDiscordHint')}</p>
        <div class="flex justify-end gap-2 mt-4">
          <button id="dc-cancel-1" class="px-3 py-1.5 rounded bg-surface-600 text-zinc-400 text-sm hover:bg-surface-500">${t('common.cancel')}</button>
          <button id="dc-next-1" class="px-4 py-1.5 rounded bg-indigo-600 text-white text-sm hover:bg-indigo-500 font-medium opacity-50 cursor-not-allowed" disabled>${t('common.next')}</button>
        </div>
      </div>

      <!-- Bot Step 2: Invite bot -->
      <div id="dc-bot-step-2" class="hidden">
        <div class="text-[10px] text-zinc-600 mb-3">${t('channels.discordStep', {n: '2', total: '4'})}</div>
        <div class="flex flex-col items-center py-4">
          <div class="w-14 h-14 rounded-full bg-indigo-500/15 flex items-center justify-center mb-4">
            <svg class="w-7 h-7 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                d="M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z"/>
            </svg>
          </div>
          <p class="text-xs text-zinc-300 text-center font-medium">${t('channels.inviteBotToServer')}</p>
          <p id="dc-bot-name" class="text-sm text-indigo-400 font-semibold mt-1"></p>
          <a id="dc-invite-link" href="#" target="_blank"
            class="mt-3 px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium inline-flex items-center gap-2 transition-colors">
            ${t('channels.openInviteLink')}
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>
          </a>
          <p class="text-[10px] text-zinc-500 mt-3 text-center">${t('channels.inviteBotDesc')}</p>
        </div>
        <div class="flex justify-end gap-2 mt-2">
          <button id="dc-back-2" class="px-3 py-1.5 rounded bg-surface-600 text-zinc-400 text-sm hover:bg-surface-500">${t('common.back')}</button>
          <button id="dc-next-2" class="px-4 py-1.5 rounded bg-indigo-600 text-white text-sm hover:bg-indigo-500 font-medium">${t('common.next')}</button>
        </div>
      </div>

      <!-- Bot Step 3: Select server & channel -->
      <div id="dc-bot-step-3" class="hidden">
        <div class="text-[10px] text-zinc-600 mb-3">${t('channels.discordStep', {n: '3', total: '4'})}</div>
        <div class="space-y-3">
          <div>
            <div class="flex items-center justify-between mb-1.5">
              <label class="text-[11px] text-zinc-400">${t('channels.selectServer')}</label>
              <button id="dc-refresh-guilds" class="text-[10px] text-indigo-400 hover:text-indigo-300">${t('channels.refreshServers')}</button>
            </div>
            <select id="dc-guild-select"
              class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2.5 text-sm text-zinc-200 focus:outline-none focus:border-zinc-500">
              <option value="">${t('channels.loadingServers')}</option>
            </select>
          </div>
          <div>
            <label class="block text-[11px] text-zinc-400 mb-1.5">${t('channels.selectChannel')}</label>
            <select id="dc-channel-select"
              class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2.5 text-sm text-zinc-200 focus:outline-none focus:border-zinc-500" disabled>
              <option value="">—</option>
            </select>
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-4">
          <button id="dc-back-3" class="px-3 py-1.5 rounded bg-surface-600 text-zinc-400 text-sm hover:bg-surface-500">${t('common.back')}</button>
          <button id="dc-next-3" class="px-4 py-1.5 rounded bg-indigo-600 text-white text-sm hover:bg-indigo-500 font-medium opacity-50 cursor-not-allowed" disabled>${t('common.next')}</button>
        </div>
      </div>

      <!-- Bot Step 4: Connected -->
      <div id="dc-bot-step-4" class="hidden">
        <div class="flex flex-col items-center py-6">
          <div class="w-16 h-16 rounded-full bg-emerald-500/20 flex items-center justify-center mb-3">
            <svg class="w-8 h-8 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
            </svg>
          </div>
          <p class="text-sm font-semibold text-emerald-400">${t('channels.discordConnected')}</p>
          <p class="text-[11px] text-zinc-400 mt-1">${t('channels.discordConnectedDesc')}</p>
          <div id="dc-test-result" class="text-[10px] text-zinc-500 mt-3"></div>
        </div>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  const $ = (sel) => overlay.querySelector(sel);
  let currentMode = 'bot';
  let validatedToken = '';
  let botName = '';
  let inviteUrl = '';
  let selectedGuildId = '';

  const cleanup = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(); });
  $('#dc-cancel-1').addEventListener('click', cleanup);

  const switchMode = (mode) => {
    currentMode = mode;
    if (mode === 'bot') {
      $('#dc-mode-bot').classList.add('border-indigo-500/50', 'bg-indigo-500/10');
      $('#dc-mode-bot').classList.remove('border-surface-600', 'bg-surface-700');
      $('#dc-mode-webhook').classList.remove('border-indigo-500/50', 'bg-indigo-500/10');
      $('#dc-mode-webhook').classList.add('border-surface-600', 'bg-surface-700');
      $('#dc-webhook-panel').classList.add('hidden');
      $('#dc-bot-step-1').classList.remove('hidden');
    } else {
      $('#dc-mode-webhook').classList.add('border-indigo-500/50', 'bg-indigo-500/10');
      $('#dc-mode-webhook').classList.remove('border-surface-600', 'bg-surface-700');
      $('#dc-mode-bot').classList.remove('border-indigo-500/50', 'bg-indigo-500/10');
      $('#dc-mode-bot').classList.add('border-surface-600', 'bg-surface-700');
      $('#dc-bot-step-1').classList.add('hidden');
      $('#dc-bot-step-2').classList.add('hidden');
      $('#dc-bot-step-3').classList.add('hidden');
      $('#dc-bot-step-4').classList.add('hidden');
      $('#dc-webhook-panel').classList.remove('hidden');
    }
  };

  $('#dc-mode-bot').addEventListener('click', () => switchMode('bot'));
  $('#dc-mode-webhook').addEventListener('click', () => switchMode('webhook'));

  const showBotStep = (step) => {
    $('#dc-bot-step-1').classList.toggle('hidden', step !== 1);
    $('#dc-bot-step-2').classList.toggle('hidden', step !== 2);
    $('#dc-bot-step-3').classList.toggle('hidden', step !== 3);
    $('#dc-bot-step-4').classList.toggle('hidden', step !== 4);
    const dots = overlay.querySelectorAll('.dc-step-dot');
    dots.forEach((d, i) => {
      d.classList.toggle('bg-indigo-400', i < step);
      d.classList.toggle('bg-surface-600', i >= step);
    });
    if (step >= 2) { $('#dc-bar-1').style.width = '100%'; $('#dc-bar-1').classList.add('bg-indigo-400'); }
    if (step >= 3) { $('#dc-bar-2').style.width = '100%'; $('#dc-bar-2').classList.add('bg-indigo-400'); }
    if (step >= 4) { $('#dc-bar-3').style.width = '100%'; $('#dc-bar-3').classList.add('bg-indigo-400'); }
    $('#dc-mode-select').classList.toggle('hidden', step > 1);
  };

  // ── Webhook flow ──────────────────────────────────
  const webhookInput = $('#dc-webhook-url');
  const webhookStatus = $('#dc-webhook-status');
  const whSaveBtn = $('#dc-wh-save');
  let whValidateTimeout = null;

  const validateWebhook = async (url) => {
    if (!url || !url.startsWith('https://discord.com/api/webhooks/')) {
      webhookStatus.classList.add('hidden');
      whSaveBtn.disabled = true;
      whSaveBtn.classList.add('opacity-50', 'cursor-not-allowed');
      return;
    }
    webhookStatus.classList.remove('hidden');
    webhookStatus.textContent = t('channels.validatingWebhook');
    webhookStatus.className = 'text-[11px] mt-2 text-zinc-400 animate-pulse';

    try {
      const res = await api.post('/api/channels/discord/validate-webhook', { webhook_url: url });
      if (res.ok) {
        webhookStatus.textContent = t('channels.webhookVerified', { name: res.name || 'webhook' });
        webhookStatus.className = 'text-[11px] mt-2 text-emerald-400';
        whSaveBtn.disabled = false;
        whSaveBtn.classList.remove('opacity-50', 'cursor-not-allowed');
      } else {
        webhookStatus.textContent = t('channels.invalidWebhook');
        webhookStatus.className = 'text-[11px] mt-2 text-red-400';
        whSaveBtn.disabled = true;
        whSaveBtn.classList.add('opacity-50', 'cursor-not-allowed');
      }
    } catch (_) {
      webhookStatus.textContent = t('channels.invalidWebhook');
      webhookStatus.className = 'text-[11px] mt-2 text-red-400';
    }
  };

  webhookInput.addEventListener('input', () => {
    if (whValidateTimeout) clearTimeout(whValidateTimeout);
    whValidateTimeout = setTimeout(() => validateWebhook(webhookInput.value.trim()), 600);
  });
  webhookInput.addEventListener('paste', () => {
    setTimeout(() => validateWebhook(webhookInput.value.trim()), 100);
  });

  $('#dc-wh-back').addEventListener('click', () => switchMode('bot'));

  whSaveBtn.addEventListener('click', async () => {
    try {
      await api.post('/api/channels/discord/configure', {
        webhook_url: webhookInput.value.trim(),
        enabled: true,
      });
      webhookStatus.textContent = t('channels.discordConnected');
      webhookStatus.className = 'text-[11px] mt-2 text-emerald-400';
      setTimeout(() => { cleanup(); render(pageContainer); }, 1200);
    } catch (e) {
      webhookStatus.textContent = e.message;
      webhookStatus.className = 'text-[11px] mt-2 text-red-400';
    }
  });

  // ── Bot flow: Step 1 — Token ──────────────────────
  const tokenInput = $('#dc-token');
  const tokenStatus = $('#dc-token-status');
  const nextBtn1 = $('#dc-next-1');
  let btValidateTimeout = null;

  const validateBotToken = async (token) => {
    if (!token) {
      tokenStatus.classList.add('hidden');
      nextBtn1.disabled = true;
      nextBtn1.classList.add('opacity-50', 'cursor-not-allowed');
      return;
    }
    tokenStatus.classList.remove('hidden');
    tokenStatus.textContent = t('channels.validatingToken');
    tokenStatus.className = 'text-[11px] mt-2 text-zinc-400 animate-pulse';

    try {
      const res = await api.post('/api/channels/discord/bot-info', { bot_token: token });
      if (res.ok) {
        validatedToken = token;
        botName = res.username;
        inviteUrl = res.invite_url;
        tokenStatus.textContent = t('channels.discordBotVerified', { username: res.username });
        tokenStatus.className = 'text-[11px] mt-2 text-emerald-400';
        nextBtn1.disabled = false;
        nextBtn1.classList.remove('opacity-50', 'cursor-not-allowed');
      } else {
        tokenStatus.textContent = t('channels.invalidBotToken');
        tokenStatus.className = 'text-[11px] mt-2 text-red-400';
        nextBtn1.disabled = true;
        nextBtn1.classList.add('opacity-50', 'cursor-not-allowed');
      }
    } catch (_) {
      tokenStatus.textContent = t('channels.invalidBotToken');
      tokenStatus.className = 'text-[11px] mt-2 text-red-400';
      nextBtn1.disabled = true;
      nextBtn1.classList.add('opacity-50', 'cursor-not-allowed');
    }
  };

  tokenInput.addEventListener('input', () => {
    if (btValidateTimeout) clearTimeout(btValidateTimeout);
    btValidateTimeout = setTimeout(() => validateBotToken(tokenInput.value.trim()), 600);
  });
  tokenInput.addEventListener('paste', () => {
    setTimeout(() => validateBotToken(tokenInput.value.trim()), 100);
  });

  nextBtn1.addEventListener('click', () => {
    if (!validatedToken) return;
    $('#dc-bot-name').textContent = botName;
    $('#dc-invite-link').href = inviteUrl;
    showBotStep(2);
  });

  // ── Bot flow: Step 2 — Invite ─────────────────────
  $('#dc-back-2').addEventListener('click', () => showBotStep(1));
  $('#dc-next-2').addEventListener('click', () => {
    showBotStep(3);
    loadGuilds();
  });

  // ── Bot flow: Step 3 — Server & Channel ───────────
  const guildSelect = $('#dc-guild-select');
  const channelSelect = $('#dc-channel-select');
  const nextBtn3 = $('#dc-next-3');

  const loadGuilds = async () => {
    guildSelect.innerHTML = `<option value="">${t('channels.loadingServers')}</option>`;
    guildSelect.disabled = true;
    channelSelect.innerHTML = '<option value="">—</option>';
    channelSelect.disabled = true;
    nextBtn3.disabled = true;
    nextBtn3.classList.add('opacity-50', 'cursor-not-allowed');

    try {
      const res = await api.post('/api/channels/discord/guilds', { bot_token: validatedToken });
      if (res.ok && res.guilds.length > 0) {
        guildSelect.innerHTML = '<option value="">' + t('channels.selectServer') + '</option>' +
          res.guilds.map(g => `<option value="${g.id}">${u.escapeHtml(g.name)}</option>`).join('');
        guildSelect.disabled = false;
      } else {
        guildSelect.innerHTML = `<option value="">${t('channels.noServers')}</option>`;
      }
    } catch (_) {
      guildSelect.innerHTML = `<option value="">${t('channels.noServers')}</option>`;
    }
  };

  $('#dc-refresh-guilds').addEventListener('click', loadGuilds);

  guildSelect.addEventListener('change', async () => {
    selectedGuildId = guildSelect.value;
    if (!selectedGuildId) {
      channelSelect.innerHTML = '<option value="">—</option>';
      channelSelect.disabled = true;
      nextBtn3.disabled = true;
      nextBtn3.classList.add('opacity-50', 'cursor-not-allowed');
      return;
    }
    channelSelect.innerHTML = `<option value="">${t('channels.loadingChannels')}</option>`;
    channelSelect.disabled = true;

    try {
      const res = await api.post('/api/channels/discord/channels', {
        bot_token: validatedToken,
        guild_id: selectedGuildId,
      });
      if (res.ok && res.channels.length > 0) {
        channelSelect.innerHTML = '<option value="">' + t('channels.selectChannel') + '</option>' +
          res.channels.map(c => `<option value="${c.id}">#${u.escapeHtml(c.name)}</option>`).join('');
        channelSelect.disabled = false;
      } else {
        channelSelect.innerHTML = '<option value="">No text channels found</option>';
      }
    } catch (_) {
      channelSelect.innerHTML = '<option value="">Error loading channels</option>';
    }
  });

  channelSelect.addEventListener('change', () => {
    if (channelSelect.value) {
      nextBtn3.disabled = false;
      nextBtn3.classList.remove('opacity-50', 'cursor-not-allowed');
    } else {
      nextBtn3.disabled = true;
      nextBtn3.classList.add('opacity-50', 'cursor-not-allowed');
    }
  });

  $('#dc-back-3').addEventListener('click', () => showBotStep(2));

  nextBtn3.addEventListener('click', async () => {
    if (!channelSelect.value) return;
    nextBtn3.disabled = true;
    nextBtn3.textContent = '...';

    try {
      await api.post('/api/channels/discord/configure', {
        bot_token: validatedToken,
        default_channel_id: channelSelect.value,
        enabled: true,
      });
    } catch (_) {}

    try {
      await api.post('/api/channels/discord/add-sender', {
        bot_token: validatedToken,
        channel_id: channelSelect.value,
      });
    } catch (_) {}

    showBotStep(4);

    const resultEl = $('#dc-test-result');
    try {
      const res = await api.post('/api/channels/discord/test', {
        message: '\u{2705} Quinely is connected to Discord! Setup complete.',
      });
      if (res.ok) {
        resultEl.textContent = 'Test message sent to your Discord channel!';
        resultEl.className = 'text-[10px] text-emerald-400/70 mt-3';
      }
    } catch (_) {}
    setTimeout(() => { cleanup(); render(pageContainer); }, 2500);
  });
}


function showConfigModal(channelId, schema, current, pageContainer) {
  const { GhostAPI: api, GhostUtils: u } = window;

  const fields = Object.entries(schema || {});
  if (fields.length === 0) {
    alert(t('channels.noSchema'));
    return;
  }

  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4';
  overlay.innerHTML = `
    <div class="bg-surface-800 rounded-xl border border-surface-600 p-6 w-full max-w-lg shadow-2xl">
      <h3 class="text-sm font-semibold text-white mb-4">${t('channels.configureChannel', { channel: channelId })}</h3>
      <div class="space-y-3" id="config-fields">
        ${fields.map(([key, spec]) => `
          <div>
            <label class="block text-[11px] text-zinc-400 mb-1">${key} ${spec.required ? '<span class="text-red-400">*</span>' : ''}</label>
            <input name="${key}" type="${spec.sensitive ? 'password' : 'text'}"
              value="${u.escapeHtml(String(current[key] || spec.default || ''))}"
              placeholder="${u.escapeHtml(spec.description || '')}"
              class="w-full bg-surface-700 border border-surface-600 rounded px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500" />
            <div class="text-[10px] text-zinc-600 mt-0.5">${u.escapeHtml(spec.description || '')}</div>
          </div>
        `).join('')}
      </div>
      <div class="flex justify-end gap-2 mt-4">
        <button id="cfg-cancel" class="px-3 py-1.5 rounded bg-surface-600 text-zinc-400 text-sm hover:bg-surface-500">${t('common.cancel')}</button>
        <button id="cfg-save" class="px-3 py-1.5 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-500 font-medium">${t('common.save')}</button>
      </div>
      <div id="cfg-result" class="text-xs mt-2 hidden"></div>
    </div>
  `;

  document.body.appendChild(overlay);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector('#cfg-cancel').addEventListener('click', () => overlay.remove());

  overlay.querySelector('#cfg-save').addEventListener('click', async () => {
    const inputs = overlay.querySelectorAll('#config-fields input');
    const configData = {};
    inputs.forEach(inp => {
      if (inp.value.trim()) configData[inp.name] = inp.value.trim();
    });

    const resultDiv = overlay.querySelector('#cfg-result');
    try {
      const res = await api.post(`/api/channels/${channelId}/configure`, configData);
      resultDiv.classList.remove('hidden');
      if (res.ok) {
        resultDiv.textContent = t('channels.configuredSuccess');
        resultDiv.className = 'text-xs text-emerald-400 mt-2';
        setTimeout(() => { overlay.remove(); render(pageContainer); }, 1000);
      } else {
        resultDiv.textContent = res.message || t('channels.configIssue');
        resultDiv.className = 'text-xs text-amber-400 mt-2';
      }
    } catch (e) {
      resultDiv.classList.remove('hidden');
      resultDiv.textContent = t('common.errorPrefix', {error: e.message});
      resultDiv.className = 'text-xs text-red-400 mt-2';
    }
  });
}
