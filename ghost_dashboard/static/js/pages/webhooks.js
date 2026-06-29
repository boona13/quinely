/** Webhooks page — Event-driven triggers for Quinely autonomy */

const t = (key, params) => window.GhostI18n?.t(key, params) ?? key;

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;

  let triggers, templates, history, configData;
  try {
    [triggers, templates, history, configData] = await Promise.all([
      api.get('/api/webhooks/triggers'),
      api.get('/api/webhooks/templates'),
      api.get('/api/webhooks/history'),
      api.get('/api/config'),
    ]);
  } catch (e) {
    container.innerHTML = `<h1 class="page-header">${t('webhooks.title')}</h1>
      <div class="stat-card"><p class="text-zinc-500 text-sm">${t('webhooks.notAvailable')}</p></div>`;
    return;
  }

  const triggerList = triggers.triggers || [];
  const templateMap = templates.templates || {};
  const events = (history.events || []).reverse();
  const secret = configData?.config?.webhook_secret || '';
  const hasSecret = secret.length > 0;

  const statusBadge = (enabled) => enabled
    ? `<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 font-medium">${t('common.enabled')}</span>`
    : `<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-zinc-600/50 text-zinc-500 font-medium">${t('common.disabled')}</span>`;

  const eventStatusColor = (s) => ({
    dispatched: 'text-blue-400', completed: 'text-emerald-400',
    auth_failed: 'text-red-400', hmac_failed: 'text-red-400',
    error: 'text-red-400', cooldown: 'text-amber-400',
    concurrency_limit: 'text-amber-400',
  }[s] || 'text-zinc-400');

  const eventStatusDot = (s) => {
    const c = {
      dispatched: 'bg-blue-400', completed: 'bg-emerald-400',
      auth_failed: 'bg-red-400', hmac_failed: 'bg-red-400',
      error: 'bg-red-400', cooldown: 'bg-amber-400',
      concurrency_limit: 'bg-amber-400',
    }[s] || 'bg-zinc-600';
    return `<span class="inline-block w-1.5 h-1.5 rounded-full ${c}"></span>`;
  };

  container.innerHTML = `
    <h1 class="page-header">${t('webhooks.title')}</h1>
    <p class="page-desc">${t('webhooks.subtitle')}</p>

    ${!hasSecret ? `
    <div class="stat-card mb-6 border border-amber-500/30">
      <div class="flex items-start gap-3">
        <svg class="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/>
        </svg>
        <div>
          <div class="text-sm font-medium text-amber-400">${t('webhooks.secretNotConfigured')}</div>
          <p class="text-xs text-zinc-400 mt-1">${t('webhooks.secretNotConfiguredDesc')}</p>
          <div class="mt-3 flex gap-2">
            <button id="wh-gen-secret" class="text-[10px] px-3 py-1.5 rounded bg-amber-500/20 text-amber-400 hover:bg-amber-500/30 font-medium">${t('webhooks.generateSecret')}</button>
          </div>
        </div>
      </div>
      <div id="wh-secret-result" class="text-xs mt-3 hidden"></div>
    </div>
    ` : ''}

    <div class="grid grid-cols-1 sm:grid-cols-4 gap-4 mb-6">
      <div class="stat-card">
        <div class="text-2xl font-bold text-white">${triggerList.length}</div>
        <div class="text-xs text-zinc-500">${t('webhooks.triggers')}</div>
      </div>
      <div class="stat-card">
        <div class="text-2xl font-bold text-emerald-400">${triggerList.filter(tr => tr.enabled).length}</div>
        <div class="text-xs text-zinc-500">${t('common.active')}</div>
      </div>
      <div class="stat-card">
        <div class="text-2xl font-bold ${hasSecret ? 'text-emerald-400' : 'text-red-400'}">${hasSecret ? t('common.active') : t('webhooks.noSecret')}</div>
        <div class="text-xs text-zinc-500">${t('webhooks.authStatus')}</div>
      </div>
      <div class="stat-card">
        <div class="text-2xl font-bold text-white">${events.length}</div>
        <div class="text-xs text-zinc-500">${t('webhooks.recentEvents')}</div>
      </div>
    </div>

    <div class="stat-card mb-6">
      <h3 class="text-sm font-semibold text-white mb-3">${t('webhooks.createTrigger')}</h3>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
        <div>
          <label class="form-label">${t('webhooks.triggerName')}</label>
          <input id="wh-name" type="text" class="form-input w-full" placeholder="${t('webhooks.triggerNamePlaceholder')}">
        </div>
        <div>
          <label class="form-label">${t('webhooks.template')}</label>
          <select id="wh-template" class="form-input w-full">
            <option value="">${t('webhooks.custom')}</option>
            ${Object.entries(templateMap).map(([id, tmpl]) =>
              `<option value="${id}">${u.escapeHtml(tmpl.name)}</option>`
            ).join('')}
          </select>
        </div>
      </div>

      <div id="wh-custom-fields">
        <div class="mb-3">
          <label class="form-label">${t('webhooks.promptTemplate')}</label>
          <textarea id="wh-prompt" class="form-input w-full h-24" placeholder="${t('webhooks.promptTemplatePlaceholder')}"></textarea>
        </div>
        <div class="mb-3">
          <label class="form-label">${t('webhooks.extractFields')} <span class="text-zinc-600">${t('webhooks.extractFieldsHint')}</span></label>
          <textarea id="wh-fields" class="form-input w-full h-16 font-mono text-xs" placeholder='{"repository": "repository.full_name", "branch": "ref"}'></textarea>
        </div>
        <div class="mb-3">
          <label class="form-label">${t('webhooks.eventType')}</label>
          <input id="wh-event-type" type="text" class="form-input w-full" placeholder="generic" value="generic">
        </div>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
        <div>
          <label class="form-label">${t('webhooks.cooldown')}</label>
          <input id="wh-cooldown" type="number" class="form-input w-full" value="30" min="0">
        </div>
        <div>
          <label class="form-label">${t('webhooks.hmacHeader')}</label>
          <input id="wh-hmac-header" type="text" class="form-input w-full" placeholder="X-Hub-Signature-256">
        </div>
        <div>
          <label class="form-label">${t('webhooks.hmacSecret')}</label>
          <input id="wh-hmac-secret" type="password" class="form-input w-full" placeholder="${t('webhooks.hmacSecretDesc')}">
        </div>
      </div>

      <button id="btn-create-wh" class="btn btn-primary">${t('webhooks.createTrigger')}</button>
      <div id="wh-create-result" class="text-xs mt-2 hidden"></div>
    </div>

    ${triggerList.length > 0 ? `
    <h3 class="text-sm font-semibold text-white mb-3">${t('webhooks.configuredTriggers')}</h3>
    <div class="space-y-3 mb-6" id="wh-trigger-list">
      ${triggerList.map(tr => `
        <div class="stat-card" data-trigger-id="${u.escapeHtml(tr.id)}">
          <div class="flex items-center justify-between mb-2">
            <div class="flex items-center gap-3">
              <div class="toggle ${tr.enabled ? 'on' : ''}" data-wh-toggle="${tr.id}"><span class="toggle-dot"></span></div>
              <span class="font-semibold text-sm text-white">${u.escapeHtml(tr.name)}</span>
              ${statusBadge(tr.enabled)}
              <span class="text-[9px] px-1.5 py-0.5 rounded bg-surface-600/50 text-zinc-500">${u.escapeHtml(tr.event_type)}</span>
            </div>
            <div class="flex gap-2">
              <button class="btn-copy-url text-[10px] px-2 py-1 rounded bg-surface-600 text-zinc-400 hover:bg-surface-500" data-id="${tr.id}">${t('webhooks.copyUrl')}</button>
              <button class="btn-test-wh text-[10px] px-2 py-1 rounded bg-blue-500/20 text-blue-400 hover:bg-blue-500/30" data-id="${tr.id}">${t('common.test')}</button>
              <button class="btn-edit-wh text-[10px] px-2 py-1 rounded bg-ghost-500/20 text-ghost-400 hover:bg-ghost-500/30" data-id="${tr.id}">${t('webhooks.editTrigger')}</button>
              <button class="btn-del-wh text-[10px] px-2 py-1 rounded bg-red-500/10 text-red-400 hover:bg-red-500/20" data-id="${tr.id}">${t('webhooks.deleteTrigger')}</button>
            </div>
          </div>
          <div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs text-zinc-400">
            <div>ID: <span class="text-zinc-300 font-mono">${u.escapeHtml(tr.id)}</span></div>
            <div>${t('webhooks.cooldownLabel')} <span class="text-zinc-300">${tr.cooldown_seconds}s</span></div>
            <div>${t('common.created')}: <span class="text-zinc-300">${tr.created_at ? u.timeAgo(tr.created_at) : '—'}</span></div>
            <div>${t('webhooks.lastFired')} <span class="text-zinc-300">${tr.last_fired > 0 ? u.timeAgo(new Date(tr.last_fired * 1000).toISOString()) : t('common.never')}</span></div>
          </div>
          <div class="mt-2">
            <div class="text-[10px] text-zinc-600 mb-1">${t('webhooks.endpointUrl')}</div>
            <code class="text-[11px] text-ghost-400 bg-surface-700 px-2 py-1 rounded block break-all">${window.location.origin}/api/webhooks/${u.escapeHtml(tr.id)}</code>
          </div>
          ${tr.hmac_header ? `<div class="mt-1 text-[10px] text-zinc-500">HMAC: <span class="text-zinc-400">${u.escapeHtml(tr.hmac_header)}</span> ${tr.hmac_secret && tr.hmac_secret !== '***' ? '' : '<span class="text-emerald-500/70">(configured)</span>'}</div>` : ''}
          <details class="mt-2">
            <summary class="text-[10px] text-zinc-600 cursor-pointer hover:text-zinc-400">${t('webhooks.promptTemplate')}</summary>
            <pre class="text-[11px] text-zinc-400 bg-surface-700 rounded p-2 mt-1 whitespace-pre-wrap max-h-32 overflow-y-auto">${u.escapeHtml(tr.prompt_template || '(none)')}</pre>
          </details>
          ${Object.keys(tr.extract_fields || {}).length > 0 ? `
          <details class="mt-1">
            <summary class="text-[10px] text-zinc-600 cursor-pointer hover:text-zinc-400">${t('webhooks.extractFields')} (${Object.keys(tr.extract_fields).length})</summary>
            <pre class="text-[11px] text-zinc-400 bg-surface-700 rounded p-2 mt-1 whitespace-pre-wrap max-h-24 overflow-y-auto">${u.escapeHtml(JSON.stringify(tr.extract_fields, null, 2))}</pre>
          </details>
          ` : ''}
        </div>
      `).join('')}
    </div>
    ` : ''}

    <div class="flex items-center justify-between mb-3">
      <h3 class="text-sm font-semibold text-zinc-400">${t('webhooks.recentEvents')}</h3>
      <button id="wh-refresh-events" class="text-[10px] px-2 py-1 rounded bg-surface-600 text-zinc-400 hover:bg-surface-500">${t('common.refresh')}</button>
    </div>
    <div id="wh-history" class="stat-card mb-6">
      ${_renderEvents(events, u)}
    </div>

    <div class="stat-card">
      <h3 class="text-sm font-semibold text-zinc-400 mb-3">${t('webhooks.integrationGuide')}</h3>
      <div class="text-xs text-zinc-500 space-y-2">
        <p>${t('webhooks.integrationDesc')}</p>
        <p>${t('webhooks.authHeader')}</p>
        <details>
          <summary class="cursor-pointer hover:text-zinc-300 font-medium">${t('webhooks.exampleCurl')}</summary>
          <pre class="bg-surface-700 rounded p-3 mt-2 text-[11px] text-zinc-400 whitespace-pre-wrap">curl -X POST ${window.location.origin}/api/webhooks/YOUR_TRIGGER_ID \\
  -H "Authorization: Bearer YOUR_SECRET" \\
  -H "Content-Type: application/json" \\
  -d '{"key": "value"}'</pre>
        </details>
        <details>
          <summary class="cursor-pointer hover:text-zinc-300 font-medium">${t('webhooks.exampleGithub')}</summary>
          <div class="bg-surface-700 rounded p-3 mt-2 text-[11px] text-zinc-400 space-y-1">
            <p>${t('webhooks.githubStep1')}</p>
            <p>${t('webhooks.githubStep2')}</p>
            <p>${t('webhooks.githubStep3')}</p>
            <p>${t('webhooks.githubStep4')}</p>
            <p>${t('webhooks.githubStep5')}</p>
            <p>${t('webhooks.githubStep6')}</p>
          </div>
        </details>
        <details>
          <summary class="cursor-pointer hover:text-zinc-300 font-medium">${t('webhooks.exampleStripe')}</summary>
          <div class="bg-surface-700 rounded p-3 mt-2 text-[11px] text-zinc-400 space-y-1">
            <p>${t('webhooks.stripeStep1')}</p>
            <p>${t('webhooks.stripeStep2')}</p>
            <p>${t('webhooks.stripeStep3')}</p>
            <p>${t('webhooks.stripeStep4')}</p>
            <p>${t('webhooks.stripeStep5')}</p>
          </div>
        </details>
      </div>
    </div>

    <div id="wh-edit-modal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/60">
      <div class="bg-surface-800 rounded-xl border border-surface-600 p-6 w-full max-w-lg mx-4 max-h-[85vh] overflow-y-auto">
        <h3 class="text-sm font-semibold text-white mb-4">${t('webhooks.editTriggerTitle')}</h3>
        <input type="hidden" id="edit-wh-id">
        <div class="space-y-3">
          <div>
            <label class="form-label">${t('webhooks.triggerName')}</label>
            <input id="edit-wh-name" type="text" class="form-input w-full">
          </div>
          <div>
            <label class="form-label">${t('webhooks.promptTemplate')}</label>
            <textarea id="edit-wh-prompt" class="form-input w-full h-28"></textarea>
          </div>
          <div>
            <label class="form-label">${t('webhooks.extractFields')} (JSON)</label>
            <textarea id="edit-wh-fields" class="form-input w-full h-20 font-mono text-xs"></textarea>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="form-label">${t('webhooks.cooldown')}</label>
              <input id="edit-wh-cooldown" type="number" class="form-input w-full" min="0">
            </div>
            <div>
              <label class="form-label">${t('webhooks.hmacHeader')}</label>
              <input id="edit-wh-hmac-header" type="text" class="form-input w-full" placeholder="X-Hub-Signature-256">
            </div>
          </div>
          <div>
            <label class="form-label">${t('webhooks.hmacSecret')} <span class="text-zinc-600">${t('webhooks.leaveEmptyKeep')}</span></label>
            <input id="edit-wh-hmac-secret" type="password" class="form-input w-full" placeholder="${t('webhooks.leaveEmptyKeep')}">
          </div>
        </div>
        <div class="flex justify-end gap-3 mt-4">
          <button id="edit-wh-cancel" class="text-xs px-4 py-2 rounded bg-surface-600 text-zinc-400 hover:bg-surface-500">${t('common.cancel')}</button>
          <button id="edit-wh-save" class="text-xs px-4 py-2 rounded bg-ghost-600 text-white hover:bg-ghost-500 font-medium">${t('webhooks.saveChanges')}</button>
        </div>
      </div>
    </div>
  `;

  // ── Generate secret ──
  container.querySelector('#wh-gen-secret')?.addEventListener('click', async () => {
    const btn = container.querySelector('#wh-gen-secret');
    btn.disabled = true;
    btn.textContent = t('webhooks.generating');
    try {
      const chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
      let newSecret = 'ghst_wh_';
      const arr = new Uint8Array(32);
      crypto.getRandomValues(arr);
      for (const b of arr) newSecret += chars[b % chars.length];

      await api.post('/api/config', { webhook_secret: newSecret });

      const resultDiv = container.querySelector('#wh-secret-result');
      resultDiv.classList.remove('hidden');
      resultDiv.className = 'text-xs mt-3 text-emerald-400';
      resultDiv.innerHTML = `${t('webhooks.secretSet')} <code class="bg-surface-700 px-1.5 py-0.5 rounded text-ghost-400 select-all">${u.escapeHtml(newSecret)}</code>`;
      btn.textContent = t('common.done');
      setTimeout(() => render(container), 3000);
    } catch (e) {
      const resultDiv = container.querySelector('#wh-secret-result');
      resultDiv.classList.remove('hidden');
      resultDiv.className = 'text-xs mt-3 text-red-400';
      resultDiv.textContent = t('common.errorPrefix', {error: e.message});
      btn.disabled = false;
      btn.textContent = t('webhooks.generateSecret');
    }
  });

  // ── Template toggle ──
  const tmplSelect = container.querySelector('#wh-template');
  const customFields = container.querySelector('#wh-custom-fields');
  tmplSelect?.addEventListener('change', () => {
    customFields.classList.toggle('hidden', !!tmplSelect.value);
  });

  // ── Create trigger ──
  container.querySelector('#btn-create-wh')?.addEventListener('click', async () => {
    const name = container.querySelector('#wh-name').value.trim();
    if (!name) { u.toast(t('webhooks.nameRequired'), 'error'); return; }

    const templateId = container.querySelector('#wh-template').value;
    const resultDiv = container.querySelector('#wh-create-result');

    let body;
    if (templateId) {
      body = {
        name,
        template_id: templateId,
        cooldown_seconds: parseInt(container.querySelector('#wh-cooldown').value) || 30,
        hmac_header: container.querySelector('#wh-hmac-header').value.trim(),
        hmac_secret: container.querySelector('#wh-hmac-secret').value,
      };
    } else {
      const prompt = container.querySelector('#wh-prompt').value.trim();
      if (!prompt) { u.toast(t('webhooks.promptRequired'), 'error'); return; }

      let extractFields = {};
      const fieldsText = container.querySelector('#wh-fields').value.trim();
      if (fieldsText) {
        try {
          extractFields = JSON.parse(fieldsText);
        } catch {
          u.toast(t('webhooks.extractFieldsJson'), 'error');
          return;
        }
      }

      body = {
        name,
        prompt_template: prompt,
        event_type: container.querySelector('#wh-event-type').value || 'generic',
        extract_fields: extractFields,
        cooldown_seconds: parseInt(container.querySelector('#wh-cooldown').value) || 30,
        hmac_header: container.querySelector('#wh-hmac-header').value.trim(),
        hmac_secret: container.querySelector('#wh-hmac-secret').value,
      };
    }

    const btn = container.querySelector('#btn-create-wh');
    btn.disabled = true;
    btn.textContent = t('webhooks.creating');

    try {
      const res = await api.post('/api/webhooks/triggers', body);
      if (res.ok) {
        u.toast(t('webhooks.triggerCreated', { name }));
        render(container);
      } else {
        resultDiv.classList.remove('hidden');
        resultDiv.className = 'text-xs mt-2 text-red-400';
        resultDiv.textContent = res.error || t('webhooks.failedCreate');
        btn.disabled = false;
        btn.textContent = t('webhooks.createTrigger');
      }
    } catch (e) {
      resultDiv.classList.remove('hidden');
      resultDiv.className = 'text-xs mt-2 text-red-400';
      resultDiv.textContent = t('common.errorPrefix', {error: e.message});
      btn.disabled = false;
      btn.textContent = t('webhooks.createTrigger');
    }
  });

  // ── Toggle enable/disable ──
  container.querySelectorAll('[data-wh-toggle]').forEach(el => {
    el.addEventListener('click', async () => {
      const id = el.dataset.whToggle;
      const isOn = el.classList.contains('on');
      try {
        const res = await api.patch(`/api/webhooks/triggers/${id}`, { enabled: !isOn });
        if (res.ok) {
          u.toast(isOn ? t('webhooks.triggerDisabled') : t('webhooks.triggerEnabled'));
          render(container);
        }
      } catch (e) {
        u.toast(t('common.errorPrefix', {error: e.message}), 'error');
      }
    });
  });

  // ── Copy URL ──
  container.querySelectorAll('.btn-copy-url').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.id;
      const url = `${window.location.origin}/api/webhooks/${id}`;
      navigator.clipboard.writeText(url).then(() => {
        btn.textContent = t('webhooks.copied');
        setTimeout(() => { btn.textContent = t('webhooks.copyUrl'); }, 1500);
      });
    });
  });

  // ── Test trigger ──
  container.querySelectorAll('.btn-test-wh').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      btn.textContent = t('common.testing');
      btn.disabled = true;
      try {
        const cfgRes = await api.get('/api/config');
        const sec = cfgRes?.config?.webhook_secret || '';
        const res = await api.postRaw(`/api/webhooks/${id}`, {
          test: true,
          message: 'Dashboard test event',
          timestamp: new Date().toISOString(),
        }, { 'Authorization': `Bearer ${sec}` });
        btn.textContent = res.ok ? t('webhooks.fired') : t('webhooks.testFailed');
        if (res.ok) u.toast(t('webhooks.testFiredMsg'));
        else u.toast(res.error || t('webhooks.testFailed'), 'error');
      } catch (e) {
        btn.textContent = t('common.error');
        u.toast(t('common.errorPrefix', {error: e.message}), 'error');
      }
      setTimeout(() => { btn.textContent = t('common.test'); btn.disabled = false; }, 2000);
    });
  });

  // ── Delete trigger ──
  container.querySelectorAll('.btn-del-wh').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      if (!confirm(t('webhooks.deleteConfirm', { name: id }))) return;
      try {
        await api.del(`/api/webhooks/triggers/${id}`);
        u.toast(t('webhooks.triggerDeleted'));
        render(container);
      } catch (e) {
        u.toast(t('common.errorPrefix', {error: e.message}), 'error');
      }
    });
  });

  // ── Edit trigger ──
  const editModal = container.querySelector('#wh-edit-modal');

  container.querySelectorAll('.btn-edit-wh').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.id;
      const trigger = triggerList.find(tr => tr.id === id);
      if (!trigger) return;

      container.querySelector('#edit-wh-id').value = trigger.id;
      container.querySelector('#edit-wh-name').value = trigger.name;
      container.querySelector('#edit-wh-prompt').value = trigger.prompt_template || '';
      container.querySelector('#edit-wh-fields').value =
        Object.keys(trigger.extract_fields || {}).length > 0
          ? JSON.stringify(trigger.extract_fields, null, 2)
          : '';
      container.querySelector('#edit-wh-cooldown').value = trigger.cooldown_seconds;
      container.querySelector('#edit-wh-hmac-header').value = trigger.hmac_header || '';
      container.querySelector('#edit-wh-hmac-secret').value = '';

      editModal.classList.remove('hidden');
      editModal.classList.add('flex');
    });
  });

  container.querySelector('#edit-wh-cancel')?.addEventListener('click', () => {
    editModal.classList.add('hidden');
    editModal.classList.remove('flex');
  });

  editModal?.addEventListener('click', (e) => {
    if (e.target === editModal) {
      editModal.classList.add('hidden');
      editModal.classList.remove('flex');
    }
  });

  container.querySelector('#edit-wh-save')?.addEventListener('click', async () => {
    const id = container.querySelector('#edit-wh-id').value;
    const saveBtn = container.querySelector('#edit-wh-save');
    saveBtn.disabled = true;
    saveBtn.textContent = t('common.saving');

    const updates = {
      name: container.querySelector('#edit-wh-name').value.trim(),
      prompt_template: container.querySelector('#edit-wh-prompt').value,
      cooldown_seconds: parseInt(container.querySelector('#edit-wh-cooldown').value) || 30,
      hmac_header: container.querySelector('#edit-wh-hmac-header').value.trim(),
    };

    const fieldsText = container.querySelector('#edit-wh-fields').value.trim();
    if (fieldsText) {
      try {
        updates.extract_fields = JSON.parse(fieldsText);
      } catch {
        u.toast(t('webhooks.extractFieldsJson'), 'error');
        saveBtn.disabled = false;
        saveBtn.textContent = t('webhooks.saveChanges');
        return;
      }
    } else {
      updates.extract_fields = {};
    }

    const hmacSecret = container.querySelector('#edit-wh-hmac-secret').value;
    if (hmacSecret) {
      updates.hmac_secret = hmacSecret;
    }

    try {
      const res = await api.patch(`/api/webhooks/triggers/${id}`, updates);
      if (res.ok) {
        u.toast(t('webhooks.triggerUpdated'));
        editModal.classList.add('hidden');
        editModal.classList.remove('flex');
        render(container);
      } else {
        u.toast(res.error || t('webhooks.failedUpdate'), 'error');
        saveBtn.disabled = false;
        saveBtn.textContent = t('webhooks.saveChanges');
      }
    } catch (e) {
      u.toast(t('common.errorPrefix', {error: e.message}), 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = t('webhooks.saveChanges');
    }
  });

  // ── Refresh events ──
  container.querySelector('#wh-refresh-events')?.addEventListener('click', async () => {
    const btn = container.querySelector('#wh-refresh-events');
    btn.textContent = t('common.refreshing');
    btn.disabled = true;
    try {
      const freshHistory = await api.get('/api/webhooks/history');
      const freshEvents = (freshHistory.events || []).reverse();
      const historyDiv = container.querySelector('#wh-history');
      if (historyDiv) historyDiv.innerHTML = _renderEvents(freshEvents, u);
      u.toast(t('webhooks.eventsRefreshed'));
    } catch (e) {
      u.toast(t('common.errorPrefix', {error: e.message}), 'error');
    }
    btn.textContent = t('common.refresh');
    btn.disabled = false;
  });
}

function _renderEvents(events, u) {
  if (events.length === 0) {
    return `<div class="text-xs text-zinc-600 py-4 text-center">${t('webhooks.noEvents')}</div>`;
  }
  return events.slice(0, 50).map(e => `
    <div class="flex items-center gap-2 py-2 border-b border-surface-600/30 last:border-0">
      ${_eventDot(e.status)}
      <span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-600 text-zinc-400 font-mono">${u.escapeHtml(e.trigger_id)}</span>
      <span class="text-[11px] ${_eventColor(e.status)} font-medium">${u.escapeHtml(e.status)}</span>
      <span class="text-[11px] text-zinc-500 flex-1 truncate">${u.escapeHtml(e.detail || '')}</span>
      <span class="text-[10px] text-zinc-600">${e.timestamp ? u.timeAgo(e.timestamp) : ''}</span>
    </div>
  `).join('');
}

function _eventColor(s) {
  return ({
    dispatched: 'text-blue-400', completed: 'text-emerald-400',
    auth_failed: 'text-red-400', hmac_failed: 'text-red-400',
    error: 'text-red-400', cooldown: 'text-amber-400',
    concurrency_limit: 'text-amber-400',
  })[s] || 'text-zinc-400';
}

function _eventDot(s) {
  const c = ({
    dispatched: 'bg-blue-400', completed: 'bg-emerald-400',
    auth_failed: 'bg-red-400', hmac_failed: 'bg-red-400',
    error: 'bg-red-400', cooldown: 'bg-amber-400',
    concurrency_limit: 'bg-amber-400',
  })[s] || 'bg-zinc-600';
  return `<span class="inline-block w-1.5 h-1.5 rounded-full ${c}"></span>`;
}
