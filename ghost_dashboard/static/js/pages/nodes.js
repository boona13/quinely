/** GhostNodes management page — browse, install, enable/disable AI nodes + GPU status */

import { isModelExt, renderPreview as render3DPreview, openFullscreen as open3DViewer } from '../three-viewer.js';

const t = (key, params) => window.GhostI18n?.t(key, params) ?? key;

const CATEGORY_ICONS = {
  image_generation: '🎨',
  video: '🎬',
  audio: '🎵',
  vision: '👁',
  llm: '🧠',
  '3d': '📐',
  data: '📊',
  utility: '🔧',
};

function getCategoryLabel(cat) {
  const key = 'nodes.cat_' + cat;
  const val = t(key);
  return val !== key ? val : cat.charAt(0).toUpperCase() + cat.slice(1).replace(/_/g, ' ');
}

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;

  let nodesData, gpuData;
  try {
    [nodesData, gpuData] = await Promise.all([
      api.get('/api/nodes'),
      api.get('/api/gpu/status'),
    ]);
  } catch (e) {
    container.innerHTML = `<div class="text-red-400 p-4">${t('nodes.loadError')}: ${u.escapeHtml(e.message)}</div>`;
    return;
  }

  const nodes = nodesData.nodes || [];
  const categories = nodesData.categories || {};
  const gpu = gpuData.device || {};
  const loadedModels = gpuData.loaded_models || [];

  container.innerHTML = `
    <div class="flex items-center justify-between mb-1">
      <h1 class="page-header">${t('nodes.title')}</h1>
      <div class="flex gap-2 items-center">
        <span class="badge badge-green">${nodes.filter(n => n.loaded).length} ${t('nodes.loaded')}</span>
        <span class="badge badge-zinc">${nodes.length} ${t('nodes.installed')}</span>
      </div>
    </div>
    <p class="page-desc">${t('nodes.subtitle')}</p>

    <!-- GPU Status Card -->
    <div class="stat-card mb-6">
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-sm font-semibold text-white">${t('nodes.gpuStatus')}</h2>
        <span class="text-xs px-2 py-0.5 rounded-full ${gpu.has_cuda ? 'bg-emerald-500/20 text-emerald-400' : gpu.has_mlx ? 'bg-purple-500/20 text-purple-400' : gpu.has_mps ? 'bg-blue-500/20 text-blue-400' : 'bg-zinc-700 text-zinc-400'}">
          ${gpu.has_cuda ? t('nodes.gpuCuda') + ': ' + u.escapeHtml(gpu.cuda_device_name || 'GPU') : gpu.has_mlx ? t('nodes.gpuMlx') + ' v' + u.escapeHtml(gpu.mlx_version || '') : gpu.has_mps ? t('nodes.gpuMps') : t('nodes.gpuCpuOnly')}
        </span>
      </div>
      ${gpuData.budget_gb > 0 ? `
        <div class="mb-2">
          <div class="flex justify-between text-xs text-zinc-400 mb-1">
            <span>${gpu.has_cuda ? t('nodes.vramUsage') : t('nodes.memoryUsage')}</span>
            <span>${gpuData.used_gb?.toFixed(1) || 0} / ${gpuData.budget_gb?.toFixed(1) || 0} GB${gpu.unified_memory_gb ? ` (${t('nodes.unified')})` : ''}</span>
          </div>
          <div class="w-full bg-surface-800 rounded-full h-2" role="progressbar" aria-valuenow="${gpuData.used_gb?.toFixed(1) || 0}" aria-valuemin="0" aria-valuemax="${gpuData.budget_gb?.toFixed(1) || 0}" aria-label="${gpu.has_cuda ? t('nodes.vramUsage') : t('nodes.memoryUsage')}">
            <div class="${(gpuData.used_gb / gpuData.budget_gb) > 0.85 ? 'bg-red-500' : (gpuData.used_gb / gpuData.budget_gb) > 0.65 ? 'bg-amber-500' : 'bg-ghost-500'} h-2 rounded-full transition-all" style="width: ${gpuData.budget_gb ? Math.min(100, (gpuData.used_gb / gpuData.budget_gb) * 100) : 0}%"></div>
          </div>
        </div>
      ` : ''}
      ${loadedModels.length > 0 ? `
        <div class="mt-3">
          <div class="text-xs text-zinc-500 mb-2">${t('nodes.loadedModels')}</div>
          <div class="space-y-1">
            ${loadedModels.map(m => `
              <div class="flex items-center justify-between bg-surface-800 rounded px-3 py-1.5">
                <span class="text-xs text-zinc-300 font-mono">${u.escapeHtml(m.model_id)}</span>
                <div class="flex items-center gap-2">
                  <span class="text-xs text-zinc-500">${m.vram_gb?.toFixed(1) || '?'} GB</span>
                  <span class="text-xs text-zinc-600">${m.device}</span>
                  <button class="text-xs text-red-400 hover:text-red-300 unload-btn" data-model="${u.escapeHtml(m.model_id)}" aria-label="${t('nodes.unload')} ${u.escapeHtml(m.model_id)}">${t('nodes.unload')}</button>
                </div>
              </div>
            `).join('')}
          </div>
        </div>
      ` : ''}
    </div>

    <!-- Category Filter -->
    <div class="flex gap-2 mb-6 flex-wrap" role="tablist" aria-label="${t('nodes.categoryFilter')}">
      <button class="node-cat-btn active px-3 py-1.5 text-xs rounded-full bg-ghost-600 text-white" data-cat="all" role="tab" aria-selected="true">${t('common.all')} (${nodes.length})</button>
      ${Object.entries(categories).map(([cat, count]) => `
        <button class="node-cat-btn px-3 py-1.5 text-xs rounded-full bg-surface-700 text-zinc-400 hover:bg-surface-600" data-cat="${cat}" role="tab" aria-selected="false">
          <span aria-hidden="true">${CATEGORY_ICONS[cat] || '📦'}</span> ${getCategoryLabel(cat)} (${count})
        </button>
      `).join('')}
    </div>

    <!-- Nodes Grid -->
    <div id="nodes-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      ${nodes.length === 0 ? `
        <div class="col-span-full text-center py-12 text-zinc-500">
          <div class="text-4xl mb-3">🧩</div>
          <div>${t('nodes.empty')}</div>
          <div class="text-xs mt-1">${t('nodes.emptyHint')}</div>
        </div>
      ` : nodes.map(n => renderNodeCard(n, u)).join('')}
    </div>

    <!-- Install Section -->
    <div class="mt-8 stat-card">
      <h2 class="text-sm font-semibold text-white mb-3">${t('nodes.installNode')}</h2>
      <div class="flex gap-2 mb-4" role="tablist" aria-label="${t('nodes.installMethod')}">
        <button class="install-method-tab px-4 py-1.5 text-xs rounded-full bg-ghost-600 text-white transition-colors" data-method="github" role="tab" aria-selected="true">${t('nodes.fromGithub')}</button>
        <button class="install-method-tab px-4 py-1.5 text-xs rounded-full bg-surface-700 text-zinc-400 hover:bg-surface-600 transition-colors" data-method="computer" role="tab" aria-selected="false">${t('nodes.fromComputer')}</button>
      </div>

      <div id="install-github-panel" role="tabpanel">
        <div class="flex gap-3">
          <input id="install-source" type="text" class="form-input flex-1" placeholder="${t('nodes.githubUrlPlaceholder')}" aria-label="${t('nodes.githubUrlPlaceholder')}">
          <button id="install-github-btn" class="btn btn-primary btn-sm">${t('nodes.install')}</button>
        </div>
        <p class="text-[10px] text-zinc-600 mt-1.5">${t('nodes.githubHint')}</p>
      </div>

      <div id="install-computer-panel" role="tabpanel" style="display:none">
        <div id="install-dropzone" class="border-2 border-dashed border-surface-600 rounded-lg p-6 text-center hover:border-ghost-500/50 transition-colors cursor-pointer">
          <div class="text-2xl mb-2" aria-hidden="true">📦</div>
          <div class="text-sm text-zinc-300 mb-1" id="install-file-label">${t('nodes.chooseZip')}</div>
          <div class="text-[10px] text-zinc-600">${t('nodes.dropHint')}</div>
          <input id="install-file-input" type="file" accept=".zip" class="hidden" aria-label="${t('nodes.chooseZip')}">
        </div>
        <div class="flex items-center justify-between mt-3">
          <span id="install-file-name" class="text-xs text-zinc-500 truncate max-w-[70%]"></span>
          <button id="install-upload-btn" class="btn btn-primary btn-sm" disabled>${t('nodes.install')}</button>
        </div>
      </div>

      <div id="install-status" class="mt-3 text-xs text-zinc-500 hidden"></div>
    </div>
  `;

  container.querySelectorAll('.unload-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const modelId = btn.dataset.model;
      btn.disabled = true;
      btn.textContent = t('nodes.unloading');
      try {
        await api.post('/api/gpu/unload', { model_id: modelId });
        u.toast(t('nodes.unloaded', { name: modelId }), 'success');
        render(container);
      } catch (e) {
        u.toast(e.message || t('nodes.unloadFailed'), 'error');
        btn.disabled = false;
        btn.textContent = t('nodes.unload');
      }
    });
  });

  container.querySelectorAll('.node-toggle-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.node;
      const action = btn.dataset.action;
      btn.disabled = true;
      btn.textContent = action === 'enable' ? t('nodes.enabling') : t('nodes.disabling');
      try {
        const result = await api.post(`/api/nodes/${name}/${action}`);
        if (result.ok) {
          u.toast(action === 'enable' ? t('nodes.statusEnabled') : t('nodes.statusDisabled'), 'success');
        } else {
          u.toast(result.error || t('common.error'), 'error');
        }
        render(container);
      } catch (e) {
        u.toast(e.message || t('common.error'), 'error');
        btn.disabled = false;
        btn.textContent = action === 'enable' ? t('nodes.enable') : t('nodes.disable');
      }
    });
  });

  container.querySelectorAll('.node-cat-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      container.querySelectorAll('.node-cat-btn').forEach(b => {
        b.classList.remove('active', 'bg-ghost-600', 'text-white');
        b.classList.add('bg-surface-700', 'text-zinc-400');
        b.setAttribute('aria-selected', 'false');
      });
      btn.classList.add('active', 'bg-ghost-600', 'text-white');
      btn.classList.remove('bg-surface-700', 'text-zinc-400');
      btn.setAttribute('aria-selected', 'true');

      const cat = btn.dataset.cat;
      container.querySelectorAll('.node-card').forEach(card => {
        const visible = cat === 'all' || card.dataset.category === cat;
        card.style.display = visible ? '' : 'none';
        card.setAttribute('aria-hidden', !visible);
      });
    });
  });

  /* ── Install method tabs ───────────────────────────────────── */
  const githubPanel = container.querySelector('#install-github-panel');
  const computerPanel = container.querySelector('#install-computer-panel');

  container.querySelectorAll('.install-method-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      container.querySelectorAll('.install-method-tab').forEach(b => {
        b.classList.remove('bg-ghost-600', 'text-white');
        b.classList.add('bg-surface-700', 'text-zinc-400');
        b.setAttribute('aria-selected', 'false');
      });
      tab.classList.add('bg-ghost-600', 'text-white');
      tab.classList.remove('bg-surface-700', 'text-zinc-400');
      tab.setAttribute('aria-selected', 'true');

      const method = tab.dataset.method;
      githubPanel.style.display = method === 'github' ? '' : 'none';
      computerPanel.style.display = method === 'computer' ? '' : 'none';
    });
  });

  /* ── GitHub URL install ──────────────────────────────────── */
  const installGithubBtn = container.querySelector('#install-github-btn');
  const installInput = container.querySelector('#install-source');
  const installStatus = container.querySelector('#install-status');

  if (installGithubBtn) {
    installGithubBtn.addEventListener('click', async () => {
      const source = installInput.value.trim();
      if (!source) { installInput.focus(); return; }
      installGithubBtn.disabled = true;
      installGithubBtn.textContent = t('nodes.installing');
      installInput.disabled = true;
      if (installStatus) {
        installStatus.classList.remove('hidden');
        installStatus.textContent = t('nodes.installProgress');
      }
      try {
        const result = await api.post('/api/nodes/install', { source });
        if (result.status === 'ok') {
          u.toast(t('nodes.installSuccess', { name: result.name }), 'success');
          if (result.warnings?.length) {
            u.toast(t('nodes.validationWarnings') + ' ' + result.warnings.join('; '), 'warning');
          }
          render(container);
        } else {
          const detail = result.validation?.errors?.join('; ') || result.error;
          u.toast(detail || t('nodes.installFailed'), 'error');
        }
      } catch (e) {
        u.toast(e.message, 'error');
      }
      installGithubBtn.disabled = false;
      installGithubBtn.textContent = t('nodes.install');
      installInput.disabled = false;
      if (installStatus) installStatus.classList.add('hidden');
    });
  }

  /* ── Zip file upload install ─────────────────────────────── */
  const dropzone = container.querySelector('#install-dropzone');
  const fileInput = container.querySelector('#install-file-input');
  const fileLabel = container.querySelector('#install-file-label');
  const fileName = container.querySelector('#install-file-name');
  const uploadBtn = container.querySelector('#install-upload-btn');

  if (dropzone && fileInput) {
    dropzone.addEventListener('click', () => fileInput.click());

    dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropzone.classList.add('border-ghost-500/50', 'bg-ghost-500/5');
    });
    dropzone.addEventListener('dragleave', () => {
      dropzone.classList.remove('border-ghost-500/50', 'bg-ghost-500/5');
    });
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('border-ghost-500/50', 'bg-ghost-500/5');
      const file = e.dataTransfer.files[0];
      if (file && file.name.toLowerCase().endsWith('.zip')) {
        fileInput.files = e.dataTransfer.files;
        selectFile(file);
      } else {
        u.toast(t('nodes.zipOnly'), 'error');
      }
    });

    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) selectFile(fileInput.files[0]);
    });

    function selectFile(file) {
      fileName.textContent = file.name + ' (' + (file.size / 1024).toFixed(0) + ' KB)';
      fileLabel.textContent = file.name;
      uploadBtn.disabled = false;
    }

    uploadBtn.addEventListener('click', async () => {
      const file = fileInput.files[0];
      if (!file) return;
      uploadBtn.disabled = true;
      uploadBtn.textContent = t('nodes.uploading');
      if (installStatus) {
        installStatus.classList.remove('hidden');
        installStatus.textContent = t('nodes.uploading');
      }
      try {
        const form = new FormData();
        form.append('file', file);
        const resp = await fetch('/api/nodes/upload-install', { method: 'POST', body: form });
        const result = await resp.json();
        if (result.status === 'ok') {
          u.toast(t('nodes.installSuccess', { name: result.name || file.name }), 'success');
          if (result.warnings?.length) {
            u.toast(t('nodes.validationWarnings') + ' ' + result.warnings.join('; '), 'warning');
          }
          render(container);
        } else {
          const detail = result.validation?.errors?.join('; ') || result.error;
          u.toast(detail || t('nodes.installFailed'), 'error');
        }
      } catch (e) {
        u.toast(e.message || t('nodes.installFailed'), 'error');
      }
      uploadBtn.disabled = false;
      uploadBtn.textContent = t('nodes.install');
      if (installStatus) installStatus.classList.add('hidden');
    });
  }

  container.querySelectorAll('.node-run-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.node;
      btn.disabled = true;
      try {
        const data = await api.get(`/api/nodes/${name}/tools`);
        if (!data.tools?.length) {
          u.toast(t('nodes.noTools'), 'error');
          return;
        }
        await openRunModal(name, data.tools, api, u);
      } catch (e) {
        u.toast(e.message || t('common.error'), 'error');
      } finally {
        btn.disabled = false;
      }
    });
  });

  /* ── Delete node ─────────────────────────────────────────── */
  container.querySelectorAll('.node-delete-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.node;
      const hasModels = btn.dataset.hasModels === 'true';
      openDeleteModal(name, hasModels, api, u, container);
    });
  });
}

/* ── Run Modal ─────────────────────────────────────────────────── */

const FILE_FIELD_PATTERNS = /image|file|path|audio|video/i;
const SKIP_FIELDS = new Set(['filename', 'output_path', 'output_file']);

function isFileField(key, schema) {
  if (schema.type !== 'string') return false;
  // URLs and model IDs are typed text, not local file uploads.
  if (/url/i.test(key)) return false;
  if (/^model$|model_id/i.test(key)) return false;
  if (FILE_FIELD_PATTERNS.test(key)) return true;
  const desc = (schema.description || '').toLowerCase();
  return /path to|file|image|audio|video/.test(desc);
}

function resolveDefault(key, schema, saved) {
  if (saved && saved[key] !== undefined && saved[key] !== '') return saved[key];
  if (schema.default !== undefined) return schema.default;
  return undefined;
}

function buildFieldInput(key, schema, isRequired, u, saved) {
  if (SKIP_FIELDS.has(key)) return '';

  const desc = u.escapeHtml(schema.description || '');
  const label = u.escapeHtml(key.replace(/_/g, ' '));
  const req = isRequired ? ' *' : '';
  const def = resolveDefault(key, schema, saved);

  if (schema.enum) {
    const opts = schema.enum.map(v => {
      const sel = (def !== undefined && String(def) === String(v)) ? ' selected' : '';
      return `<option value="${u.escapeHtml(v)}"${sel}>${u.escapeHtml(v)}</option>`;
    }).join('');
    return `
      <div class="mb-3">
        <label class="block text-xs text-zinc-400 mb-1">${label}${req}</label>
        <select name="${key}" class="form-input w-full text-sm">${opts}</select>
        ${desc ? `<div class="text-[10px] text-zinc-600 mt-0.5">${desc}</div>` : ''}
      </div>`;
  }

  if (schema.type === 'number' || schema.type === 'integer') {
    const step = schema.type === 'integer' ? '1' : 'any';
    const val = def !== undefined ? ` value="${def}"` : '';
    return `
      <div class="mb-3">
        <label class="block text-xs text-zinc-400 mb-1">${label}${req}</label>
        <input type="number" name="${key}" step="${step}" class="form-input w-full text-sm" placeholder="${desc}"${val}>
        ${desc ? `<div class="text-[10px] text-zinc-600 mt-0.5">${desc}</div>` : ''}
      </div>`;
  }

  if (schema.type === 'boolean') {
    const chk = def ? ' checked' : '';
    return `
      <div class="mb-3 flex items-center gap-2">
        <input type="checkbox" name="${key}" class="rounded bg-surface-700 border-surface-600 text-ghost-500"${chk}>
        <label class="text-xs text-zinc-400">${label}${req}</label>
        ${desc ? `<span class="text-[10px] text-zinc-600">${desc}</span>` : ''}
      </div>`;
  }

  if (schema.type === 'array') {
    const val = (def !== undefined && def !== null) ? u.escapeHtml(JSON.stringify(def)) : '';
    return `
      <div class="mb-3">
        <label class="block text-xs text-zinc-400 mb-1">${label}${req}</label>
        <textarea name="${key}" data-array="1" rows="3" class="form-input w-full text-sm font-mono resize-y" placeholder='JSON array, e.g. ["a", "b"]'>${val}</textarea>
        ${desc ? `<div class="text-[10px] text-zinc-600 mt-0.5">${desc} — enter a JSON array</div>` : `<div class="text-[10px] text-zinc-600 mt-0.5">Enter a JSON array</div>`}
      </div>`;
  }

  if (isFileField(key, schema)) {
    const accept = /image/i.test(key) || /image/i.test(schema.description || '')
      ? 'image/*'
      : /audio/i.test(key) || /audio/i.test(schema.description || '')
        ? 'audio/*'
        : /video/i.test(key) || /video/i.test(schema.description || '')
          ? 'video/*' : '';
    const val = (def !== undefined && def !== '') ? u.escapeHtml(String(def)) : '';
    return `
      <div class="mb-3">
        <label class="block text-xs text-zinc-400 mb-1">${label}${req}</label>
        <div class="flex gap-2 items-center">
          <input type="text" name="${key}" class="form-input flex-1 text-sm" placeholder="${desc}" readonly value="${val}">
          <input type="file" class="file-native-input hidden" data-field="${key}" ${accept ? `accept="${accept}"` : ''}>
          <button type="button" class="file-browse-btn btn btn-sm text-xs bg-surface-700 text-zinc-300 hover:bg-surface-600 shrink-0" data-field="${key}">${t('nodes.browse')}</button>
        </div>
        <div class="file-upload-status text-[10px] text-zinc-600 mt-1 hidden" data-field="${key}"></div>
      </div>`;
  }

  const isPrompt = /prompt/i.test(key);
  if (isPrompt) {
    const val = (def !== undefined && def !== '') ? u.escapeHtml(String(def)) : '';
    return `
      <div class="mb-3">
        <label class="block text-xs text-zinc-400 mb-1">${label}${req}</label>
        <textarea name="${key}" rows="3" class="form-input w-full text-sm resize-y" placeholder="${desc}">${val}</textarea>
      </div>`;
  }

  const val = (def !== undefined && def !== '') ? ` value="${u.escapeHtml(String(def))}"` : '';
  return `
    <div class="mb-3">
      <label class="block text-xs text-zinc-400 mb-1">${label}${req}</label>
      <input type="text" name="${key}" class="form-input w-full text-sm" placeholder="${desc}"${val}>
      ${desc ? `<div class="text-[10px] text-zinc-600 mt-0.5">${desc}</div>` : ''}
    </div>`;
}

function buildToolForm(tool, u, saved) {
  const params = tool.parameters || {};
  const props = params.properties || {};
  const required = new Set(params.required || []);
  const keys = Object.keys(props);

  if (!keys.length) {
    return `<p class="text-xs text-zinc-500">${t('nodes.noParams')}</p>`;
  }

  const reqFields = keys.filter(k => required.has(k));
  const optFields = keys.filter(k => !required.has(k));

  let html = '';
  for (const k of reqFields) html += buildFieldInput(k, props[k], true, u, saved);
  if (optFields.length) {
    html += `
      <details class="mt-2 mb-2" open>
        <summary class="text-xs text-zinc-500 cursor-pointer hover:text-zinc-300 select-none">
          ${t('nodes.advancedOptions')} (${optFields.length})
        </summary>
        <div class="mt-2">`;
    for (const k of optFields) html += buildFieldInput(k, props[k], false, u, saved);
    html += `</div></details>`;
  }
  return html;
}

async function uploadFileForField(fileInput, textInput, statusEl, u) {
  const file = fileInput.files[0];
  if (!file) return;

  statusEl.classList.remove('hidden');
  statusEl.textContent = `${t('nodes.uploading')} ${file.name}...`;

  const formData = new FormData();
  formData.append('file', file);

  try {
    const resp = await fetch('/api/nodes/upload-file', { method: 'POST', body: formData });
    const data = await resp.json();
    if (data.ok && data.path) {
      textInput.value = data.path;
      textInput.dispatchEvent(new Event('change'));
      statusEl.textContent = `${file.name}`;
      statusEl.classList.remove('hidden');
    } else {
      statusEl.textContent = data.error || t('nodes.uploadError');
      u.toast(data.error || t('nodes.uploadError'), 'error');
    }
  } catch (e) {
    statusEl.textContent = t('nodes.uploadError');
    u.toast(t('nodes.uploadError'), 'error');
  }
}

function openImageViewer(src, u) {
  const existing = document.getElementById('image-viewer-overlay');
  if (existing) existing.remove();

  const viewer = document.createElement('div');
  viewer.id = 'image-viewer-overlay';
  viewer.style.cssText = 'position:fixed;inset:0;z-index:120;background:rgba(0,0,0,0.85);display:flex;align-items:center;justify-content:center;cursor:zoom-out';
  viewer.innerHTML = `
    <div style="position:relative;max-width:95vw;max-height:95vh">
      <img src="${src}" style="max-width:95vw;max-height:90vh;object-fit:contain;border-radius:8px" alt="Result">
      <div style="position:absolute;bottom:-36px;left:0;right:0;display:flex;justify-content:center;gap:8px">
        <a href="${src}" download class="text-xs text-zinc-400 hover:text-white bg-black/60 px-3 py-1.5 rounded-full transition-colors">${t('nodes.download')}</a>
        <button id="iv-close" class="text-xs text-zinc-400 hover:text-white bg-black/60 px-3 py-1.5 rounded-full transition-colors">${t('nodes.close')}</button>
      </div>
    </div>`;
  document.body.appendChild(viewer);

  function closeViewer() { viewer.remove(); document.removeEventListener('keydown', onKey); }
  function onKey(e) { if (e.key === 'Escape') closeViewer(); }
  document.addEventListener('keydown', onKey);
  viewer.addEventListener('click', (e) => { if (e.target === viewer) closeViewer(); });
  viewer.querySelector('#iv-close')?.addEventListener('click', closeViewer);
}

async function openRunModal(nodeName, tools, api, u) {
  const existing = document.getElementById('node-run-overlay');
  if (existing) existing.remove();

  let selectedIdx = 0;
  const savedCache = {};

  async function loadSaved(toolName) {
    if (savedCache[toolName] !== undefined) return savedCache[toolName];
    try {
      const resp = await fetch(`/api/nodes/settings/${nodeName}/${toolName}`);
      const data = await resp.json();
      savedCache[toolName] = data.settings || {};
    } catch (e) {
      savedCache[toolName] = {};
    }
    return savedCache[toolName];
  }

  const overlay = document.createElement('div');
  overlay.id = 'node-run-overlay';
  overlay.className = 'modal-overlay';
  document.body.appendChild(overlay);

  const firstSaved = await loadSaved(tools[0].name);
  overlay.innerHTML = renderModalContent(nodeName, tools, selectedIdx, u, firstSaved);

  function renderModalContent(nodeName, tools, idx, u, saved) {
    const tool = tools[idx];
    return `
      <div class="modal-panel" style="max-width:600px">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-base font-semibold text-white">${t('nodes.runTool')}</h2>
            <div class="text-xs text-zinc-500 mt-0.5">${u.escapeHtml(nodeName)}</div>
          </div>
          <button id="run-modal-close" class="text-zinc-500 hover:text-white text-lg leading-none" aria-label="${t('nodes.close')}">&times;</button>
        </div>

        ${tools.length > 1 ? `
          <div class="flex gap-1 mb-4 flex-wrap">
            ${tools.map((tt, i) => `
              <button class="run-tool-tab px-3 py-1 text-xs rounded-full transition-colors ${i === idx ? 'bg-ghost-600 text-white' : 'bg-surface-700 text-zinc-400 hover:bg-surface-600'}" data-idx="${i}">
                ${u.escapeHtml(tt.name)}
              </button>
            `).join('')}
          </div>
        ` : ''}

        <div class="text-xs text-zinc-400 mb-4 leading-relaxed">${u.escapeHtml(tool.description || '')}</div>

        <form id="run-tool-form" autocomplete="off">
          ${buildToolForm(tool, u, saved)}
          <div class="flex items-center gap-3 mt-4 pt-3 border-t border-surface-700">
            <button type="submit" id="run-submit-btn" class="btn btn-primary btn-sm text-sm px-6">${t('nodes.run')}</button>
            <button type="button" id="run-save-btn" class="btn btn-sm text-xs bg-surface-700 text-zinc-400 hover:text-zinc-200 hover:bg-surface-600 transition-colors px-4">${t('nodes.saveSettings')}</button>
            <span id="save-feedback" class="text-[10px] text-emerald-400" style="display:none">${t('nodes.saved')}</span>
          </div>
        </form>

        <div id="run-progress-area" style="display:none" class="mt-4 pt-3 border-t border-surface-700">
          <div class="flex items-center gap-3 mb-2">
            <div class="w-4 h-4 border-2 border-ghost-400 border-t-transparent rounded-full animate-spin shrink-0"></div>
            <span id="run-progress-msg" class="text-xs text-zinc-400">${t('nodes.starting')}</span>
            <span id="run-elapsed" class="text-xs text-zinc-600 ml-auto tabular-nums">0.0s</span>
          </div>
          <div class="w-full h-1.5 bg-surface-700 rounded-full overflow-hidden">
            <div id="run-progress-bar" class="h-full bg-gradient-to-r from-ghost-500 to-ghost-400 rounded-full transition-all duration-700 ease-out" style="width:2%"></div>
          </div>
        </div>

        <div id="run-result-area" style="display:none" class="mt-4 pt-3 border-t border-surface-700"></div>
      </div>`;
  }

  function collectFormValues() {
    const tool = tools[selectedIdx];
    const form = overlay.querySelector('#run-tool-form');
    if (!form) return {};
    const props = (tool.parameters || {}).properties || {};
    const vals = {};
    for (const [key, schema] of Object.entries(props)) {
      if (SKIP_FIELDS.has(key)) continue;
      const el = form.querySelector(`[name="${key}"]`);
      if (!el) continue;
      if (schema.type === 'boolean') { vals[key] = el.checked; continue; }
      const raw = el.value.trim();
      if (!raw) continue;
      if (schema.type === 'number') vals[key] = parseFloat(raw);
      else if (schema.type === 'integer') vals[key] = parseInt(raw, 10);
      else if (schema.type === 'array') { try { vals[key] = JSON.parse(raw); } catch { vals[key] = raw; } }
      else vals[key] = raw;
    }
    return vals;
  }

  function bindEvents() {
    overlay.querySelector('#run-modal-close')?.addEventListener('click', closeModal);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });

    overlay.querySelectorAll('.run-tool-tab').forEach(tab => {
      tab.addEventListener('click', async () => {
        selectedIdx = parseInt(tab.dataset.idx, 10);
        const saved = await loadSaved(tools[selectedIdx].name);
        overlay.innerHTML = renderModalContent(nodeName, tools, selectedIdx, u, saved);
        bindEvents();
      });
    });

    overlay.querySelectorAll('.file-browse-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const fieldName = btn.dataset.field;
        const fileInput = overlay.querySelector(`.file-native-input[data-field="${fieldName}"]`);
        if (fileInput) fileInput.click();
      });
    });

    overlay.querySelectorAll('.file-native-input').forEach(input => {
      input.addEventListener('change', () => {
        const fieldName = input.dataset.field;
        const textInput = overlay.querySelector(`[name="${fieldName}"]`);
        const statusEl = overlay.querySelector(`.file-upload-status[data-field="${fieldName}"]`);
        if (textInput && statusEl) uploadFileForField(input, textInput, statusEl, u);
      });
    });

    overlay.querySelector('#run-save-btn')?.addEventListener('click', async () => {
      const tool = tools[selectedIdx];
      const vals = collectFormValues();
      try {
        await api.post(`/api/nodes/settings/${nodeName}/${tool.name}`, { settings: vals });
        savedCache[tool.name] = vals;
        const fb = overlay.querySelector('#save-feedback');
        if (fb) {
          fb.style.display = 'inline';
          setTimeout(() => { fb.style.display = 'none'; }, 2000);
        }
        u.toast(t('nodes.settingsSaved'), 'success');
      } catch (e) {
        u.toast(t('nodes.settingsSaveError'), 'error');
      }
    });

    const form = overlay.querySelector('#run-tool-form');
    form?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const tool = tools[selectedIdx];
      const params = tool.parameters || {};
      const props = params.properties || {};
      const required = new Set(params.required || []);
      const args = {};

      for (const [key, schema] of Object.entries(props)) {
        if (SKIP_FIELDS.has(key)) continue;

        const el = form.querySelector(`[name="${key}"]`);
        if (!el) continue;

        if (schema.type === 'boolean') {
          args[key] = el.checked;
          continue;
        }

        let val = el.value.trim();
        if (!val) {
          if (required.has(key)) {
            el.classList.add('border-red-500');
            el.focus();
            u.toast(`${key.replace(/_/g, ' ')} ${t('nodes.isRequired')}`, 'error');
            return;
          }
          continue;
        }
        el.classList.remove('border-red-500');

        if (schema.type === 'array') {
          let parsed;
          try { parsed = JSON.parse(val); } catch { parsed = undefined; }
          if (!Array.isArray(parsed)) {
            el.classList.add('border-red-500');
            el.focus();
            u.toast(`${key.replace(/_/g, ' ')} must be a valid JSON array`, 'error');
            return;
          }
          args[key] = parsed;
          continue;
        }

        if (schema.type === 'number') val = parseFloat(val);
        else if (schema.type === 'integer') val = parseInt(val, 10);

        if ((schema.type === 'number' || schema.type === 'integer') && isNaN(val)) {
          el.classList.add('border-red-500');
          el.focus();
          u.toast(`${key.replace(/_/g, ' ')} must be a number`, 'error');
          return;
        }
        args[key] = val;
      }

      const submitBtn = overlay.querySelector('#run-submit-btn');
      const progressArea = overlay.querySelector('#run-progress-area');
      const progressMsg = overlay.querySelector('#run-progress-msg');
      const progressBar = overlay.querySelector('#run-progress-bar');
      const elapsedEl = overlay.querySelector('#run-elapsed');
      const resultArea = overlay.querySelector('#run-result-area');

      submitBtn.disabled = true;
      submitBtn.style.display = 'none';
      progressArea.style.display = 'block';
      resultArea.style.display = 'none';
      resultArea.innerHTML = '';

      let jobId = null;
      try {
        const resp = await api.post(`/api/nodes/${nodeName}/run`, { tool: tool.name, args });
        if (!resp.ok || !resp.job_id) {
          throw new Error(resp.error || t('nodes.runError'));
        }
        jobId = resp.job_id;
      } catch (err) {
        progressArea.style.display = 'none';
        submitBtn.disabled = false;
        submitBtn.style.display = '';
        resultArea.style.display = 'block';
        resultArea.innerHTML = `<div class="text-sm text-red-400">${u.escapeHtml(err.message || t('nodes.runError'))}</div>`;
        return;
      }

      const localStart = Date.now();
      let pollTimer = null;
      const POLL_MS = 1500;

      function updateLocalElapsed() {
        const s = ((Date.now() - localStart) / 1000).toFixed(1);
        if (elapsedEl) elapsedEl.textContent = s + 's';
      }
      const tickTimer = setInterval(updateLocalElapsed, 200);

      function estimateProgress(elapsed) {
        if (elapsed < 3) return 5;
        if (elapsed < 10) return 15;
        if (elapsed < 20) return 30;
        if (elapsed < 40) return 50;
        if (elapsed < 60) return 70;
        if (elapsed < 90) return 82;
        return Math.min(92, 82 + (elapsed - 90) * 0.05);
      }

      async function poll() {
        try {
          const s = await (await fetch(`/api/nodes/run-status/${jobId}`)).json();

          if (s.status === 'running') {
            if (progressMsg) progressMsg.textContent = s.message || t('nodes.running');
            const pct = estimateProgress(s.elapsed || 0);
            if (progressBar) progressBar.style.width = pct + '%';
            pollTimer = setTimeout(poll, POLL_MS);
            return;
          }

          clearInterval(tickTimer);

          if (s.status === 'complete') {
            if (progressBar) progressBar.style.width = '100%';
            if (progressMsg) progressMsg.textContent = t('nodes.runSuccess');
            const totalSec = (s.elapsed || ((Date.now() - localStart) / 1000)).toFixed(1);
            if (elapsedEl) elapsedEl.textContent = totalSec + 's';

            setTimeout(() => {
              progressArea.style.display = 'none';
              showResult(s.result, totalSec);
            }, 600);
          } else {
            progressArea.style.display = 'none';
            resultArea.style.display = 'block';
            resultArea.innerHTML = `<div class="text-sm text-red-400">${u.escapeHtml(s.error || t('nodes.runError'))}</div>`;
            submitBtn.disabled = false;
            submitBtn.style.display = '';
          }
        } catch (err) {
          clearInterval(tickTimer);
          progressArea.style.display = 'none';
          resultArea.style.display = 'block';
          resultArea.innerHTML = `<div class="text-sm text-red-400">${u.escapeHtml(err.message || t('nodes.runError'))}</div>`;
          submitBtn.disabled = false;
          submitBtn.style.display = '';
        }
      }

      function showResult(r, totalSec) {
        resultArea.style.display = 'block';
        submitBtn.disabled = false;
        submitBtn.style.display = '';

        if (!r) {
          resultArea.innerHTML = `<div class="text-sm text-red-400">${t('nodes.runError')}</div>`;
          return;
        }
        if (r.status === 'error') {
          resultArea.innerHTML = `<div class="text-sm text-red-400">${u.escapeHtml(r.error || t('nodes.runError'))}</div>`;
          return;
        }

        let html = `
          <div class="flex items-center gap-2 mb-3">
            <div class="w-5 h-5 rounded-full bg-emerald-500/20 flex items-center justify-center shrink-0">
              <svg class="w-3 h-3 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="3"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
            </div>
            <span class="text-sm text-emerald-400 font-medium">${t('nodes.runSuccess')}</span>
            <span class="text-[10px] text-zinc-600 ml-auto">${totalSec}s</span>
          </div>`;

        if (r.path) {
          const src = `/api/nodes/serve-file?path=${encodeURIComponent(r.path)}`;
          const ext = (r.path.split('.').pop() || '').toLowerCase();
          const audioExts = ['mp3', 'wav', 'ogg', 'flac', 'aac', 'm4a'];
          const videoExts = ['mp4', 'webm', 'mov', 'avi', 'mkv'];
          const modelExts = ['obj', 'glb', 'gltf', 'stl'];

          if (audioExts.includes(ext)) {
            html += `
              <div class="mb-3 bg-surface-800 rounded-lg p-3" id="result-audio-wrap">
                <div class="flex items-center gap-2 mb-2">
                  <span class="text-lg">🎵</span>
                  <span class="text-xs text-zinc-300 truncate">${u.escapeHtml(r.path.replace(/\\/g,'/').split('/').pop())}</span>
                  <a href="${src}" download class="ml-auto text-[10px] text-ghost-400 hover:text-ghost-300 transition-colors">${t('nodes.download')}</a>
                </div>
                <audio controls src="${src}" class="w-full" style="height:36px"></audio>
              </div>`;
          } else if (videoExts.includes(ext)) {
            html += `
              <div class="mb-3" id="result-video-wrap">
                <video controls src="${src}" class="rounded-lg w-full max-h-[420px] border border-surface-700 bg-black/30"></video>
                <div class="flex justify-end mt-1">
                  <a href="${src}" download class="text-[10px] text-ghost-400 hover:text-ghost-300 transition-colors">${t('nodes.download')}</a>
                </div>
              </div>`;
          } else if (modelExts.includes(ext)) {
            html += `
              <div class="mb-3 rounded-lg border border-surface-700 overflow-hidden" id="result-3d-wrap">
                <div id="result-3d-canvas" style="width:100%;height:320px;background:#1a1a2e;position:relative">
                  <div class="flex items-center justify-center h-full text-zinc-500 text-xs">${t('nodes.3dLoading')}</div>
                </div>
                <div class="flex items-center gap-2 p-2 bg-surface-800">
                  <span class="text-lg">📐</span>
                  <span class="text-xs text-zinc-300 truncate">${u.escapeHtml(r.path.replace(/\\/g,'/').split('/').pop())}</span>
                  <button id="result-3d-fullscreen" class="ml-auto text-[10px] text-ghost-400 hover:text-ghost-300 transition-colors cursor-pointer">${t('nodes.3dFullscreen')}</button>
                  <a href="${src}" download class="text-[10px] text-ghost-400 hover:text-ghost-300 transition-colors">${t('nodes.download')}</a>
                </div>
              </div>`;
          } else {
            html += `
              <div class="relative group cursor-pointer mb-3" id="result-image-wrap">
                <img src="${src}" class="rounded-lg w-full max-h-[420px] object-contain border border-surface-700 bg-black/30" alt="Result">
                <div class="absolute inset-0 bg-black/0 group-hover:bg-black/40 transition-colors rounded-lg flex items-center justify-center">
                  <span class="opacity-0 group-hover:opacity-100 transition-opacity text-white text-xs bg-black/60 px-3 py-1.5 rounded-full">${t('nodes.viewFullSize')}</span>
                </div>
              </div>`;
          }
        }

        const meta = Object.entries(r).filter(([k]) => !['status', 'path', 'raw'].includes(k));
        if (meta.length) {
          html += `<div class="text-[11px] text-zinc-500 space-y-0.5 bg-surface-800 rounded-lg p-2.5">`;
          for (const [k, v] of meta) {
            html += `<div><span class="text-zinc-600 font-medium">${u.escapeHtml(k)}:</span> ${u.escapeHtml(String(v))}</div>`;
          }
          html += `</div>`;
        }

        resultArea.innerHTML = html;

        const imgWrap = resultArea.querySelector('#result-image-wrap');
        if (imgWrap) {
          imgWrap.addEventListener('click', () => {
            const imgSrc = imgWrap.querySelector('img')?.src;
            if (!imgSrc) return;
            openImageViewer(imgSrc, u);
          });
        }

        const threeDWrap = resultArea.querySelector('#result-3d-wrap');
        if (threeDWrap && r.path) {
          const mSrc = `/api/nodes/serve-file?path=${encodeURIComponent(r.path)}`;
          const mExt = (r.path.split('.').pop() || '').toLowerCase();
          const canvasEl = threeDWrap.querySelector('#result-3d-canvas');
          const sceneHandle = render3DPreview(canvasEl, mSrc, mExt);
          threeDWrap.querySelector('#result-3d-fullscreen')?.addEventListener('click', () => {
            open3DViewer(mSrc, mExt);
          });
        }
      }

      pollTimer = setTimeout(poll, POLL_MS);
    });
  }

  function closeModal() {
    overlay.classList.add('modal-closing');
    setTimeout(() => overlay.remove(), 200);
  }

  const onEscape = (e) => {
    if (e.key === 'Escape') { closeModal(); document.removeEventListener('keydown', onEscape); }
  };
  document.addEventListener('keydown', onEscape);

  bindEvents();
}

/* ── Delete Confirmation Modal ──────────────────────────────────── */

function openDeleteModal(nodeName, hasModels, api, u, pageContainer) {
  const existing = document.getElementById('node-delete-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'node-delete-overlay';
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-panel" style="max-width:420px">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-base font-semibold text-white">${t('nodes.deleteTitle')}</h2>
        <button id="delete-modal-close" class="text-zinc-500 hover:text-white text-lg leading-none" aria-label="${t('nodes.close')}">&times;</button>
      </div>
      <p class="text-sm text-zinc-400 mb-4">
        ${t('nodes.deleteConfirm', { name: u.escapeHtml(nodeName) })}
      </p>
      ${hasModels ? `
        <label class="flex items-start gap-2.5 mb-4 p-3 rounded-lg bg-surface-800 border border-surface-700 cursor-pointer hover:border-surface-600 transition-colors">
          <input type="checkbox" id="delete-models-check" class="mt-0.5 rounded bg-surface-700 border-surface-600 text-red-500">
          <div>
            <div class="text-xs text-zinc-300">${t('nodes.deleteModels')}</div>
            <div class="text-[10px] text-zinc-500 mt-0.5">${t('nodes.deleteModelsHint')}</div>
          </div>
        </label>
      ` : ''}
      <div class="flex items-center gap-3 pt-2">
        <button id="delete-confirm-btn" class="btn btn-sm text-xs bg-red-500/20 text-red-400 hover:bg-red-500/30 border border-red-500/30 px-5 transition-colors">${t('nodes.delete')}</button>
        <button id="delete-cancel-btn" class="btn btn-sm text-xs bg-surface-700 text-zinc-400 hover:text-zinc-200 transition-colors">${t('nodes.cancel')}</button>
      </div>
    </div>`;

  document.body.appendChild(overlay);

  function close() {
    overlay.classList.add('modal-closing');
    setTimeout(() => overlay.remove(), 200);
    document.removeEventListener('keydown', onEsc);
  }
  function onEsc(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onEsc);

  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('#delete-modal-close')?.addEventListener('click', close);
  overlay.querySelector('#delete-cancel-btn')?.addEventListener('click', close);

  overlay.querySelector('#delete-confirm-btn')?.addEventListener('click', async () => {
    const deleteModels = overlay.querySelector('#delete-models-check')?.checked || false;
    const confirmBtn = overlay.querySelector('#delete-confirm-btn');
    confirmBtn.disabled = true;
    confirmBtn.textContent = t('nodes.deleting');

    try {
      const resp = await fetch(`/api/nodes/${encodeURIComponent(nodeName)}/uninstall`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delete_models: deleteModels }),
      });
      const result = await resp.json();
      if (result.ok) {
        close();
        let msg = t('nodes.deleteSuccess', { name: nodeName });
        if (result.deleted_models?.length) {
          msg += ' · ' + t('nodes.deletedModelsCount', { count: result.deleted_models.length });
        }
        u.toast(msg, 'success');
        render(pageContainer);
      } else {
        u.toast(result.error || t('nodes.deleteFailed'), 'error');
        confirmBtn.disabled = false;
        confirmBtn.textContent = t('nodes.delete');
      }
    } catch (e) {
      u.toast(e.message || t('nodes.deleteFailed'), 'error');
      confirmBtn.disabled = false;
      confirmBtn.textContent = t('nodes.delete');
    }
  });
}

function renderNodeCard(node, u) {
  const m = node.manifest || {};
  const cat = m.category || 'utility';
  const icon = CATEGORY_ICONS[cat] || '📦';
  const isLoaded = node.loaded;
  const isEnabled = node.enabled;
  const hasError = !!node.error;

  const statusColor = hasError ? 'text-red-400' : isLoaded ? 'text-emerald-400' : isEnabled ? 'text-yellow-400' : 'text-zinc-500';
  const statusText = hasError ? t('nodes.statusError') : isLoaded ? t('nodes.statusLoaded') : isEnabled ? t('nodes.statusEnabled') : t('nodes.statusDisabled');
  const statusDot = hasError ? 'bg-red-400' : isLoaded ? 'bg-emerald-400' : isEnabled ? 'bg-yellow-400' : 'bg-zinc-600';

  return `
    <div class="node-card stat-card hover:border-ghost-500/30 transition-colors" data-category="${cat}">
      <div class="flex items-start justify-between mb-2">
        <div class="flex items-center gap-2">
          <span class="text-lg" aria-hidden="true">${icon}</span>
          <div>
            <div class="text-sm font-semibold text-white truncate max-w-[160px]" title="${u.escapeHtml(node.name)}">${u.escapeHtml(node.name)}</div>
            <div class="text-xs text-zinc-500">${u.escapeHtml(m.version || '?')} · ${u.escapeHtml(m.author || t('common.unknown'))}</div>
          </div>
        </div>
        <div class="flex items-center gap-1.5" title="${statusText}">
          <span class="w-2 h-2 rounded-full ${statusDot}" aria-hidden="true"></span>
          <span class="text-xs ${statusColor}" role="status">${statusText}</span>
        </div>
      </div>
      <p class="text-xs text-zinc-400 mb-3 line-clamp-2">${u.escapeHtml(m.description || '')}</p>
      <div class="flex items-center justify-between">
        <div class="flex gap-1 flex-wrap">
          ${(m.tags || []).slice(0, 3).map(tag => `<span class="text-[10px] px-1.5 py-0.5 bg-surface-700 text-zinc-500 rounded">${u.escapeHtml(tag)}</span>`).join('')}
        </div>
        <div class="flex gap-1">
          ${m.cloud_provider ? `<span class="text-[10px] px-1.5 py-0.5 bg-sky-500/20 text-sky-400 rounded">☁️ Cloud</span>` : ''}
          ${m.requires_gpu ? `<span class="text-[10px] px-1.5 py-0.5 bg-amber-500/20 text-amber-400 rounded">${t('nodes.badgeGpu')}</span>` : ''}
          ${node.source === 'bundled' ? `<span class="text-[10px] px-1.5 py-0.5 bg-ghost-500/20 text-ghost-400 rounded">${t('nodes.badgeBundled')}</span>` : ''}
        </div>
      </div>
      ${node.tools?.length ? `<div class="mt-2 text-[10px] text-zinc-600 truncate" title="${node.tools.map(tool => u.escapeHtml(tool)).join(', ')}">${t('nodes.tools')}: ${node.tools.map(tool => u.escapeHtml(tool)).join(', ')}</div>` : ''}
      ${hasError ? `<div class="mt-2 text-[10px] text-red-400/70 truncate cursor-help" title="${u.escapeHtml(node.error)}">${u.escapeHtml(node.error)}</div>` : ''}
      <div class="mt-3 flex gap-2">
        ${isEnabled
          ? `<button class="node-toggle-btn btn btn-sm text-xs bg-surface-700 text-zinc-300 hover:bg-red-500/20 hover:text-red-400 transition-colors" data-node="${u.escapeHtml(node.name)}" data-action="disable">${t('nodes.disable')}</button>`
          : `<button class="node-toggle-btn btn btn-sm text-xs bg-surface-700 text-zinc-300 hover:bg-emerald-500/20 hover:text-emerald-400 transition-colors" data-node="${u.escapeHtml(node.name)}" data-action="enable">${t('nodes.enable')}</button>`
        }
        ${hasError ? `<button class="node-toggle-btn btn btn-sm text-xs bg-surface-700 text-zinc-300 hover:bg-amber-500/20 hover:text-amber-400 transition-colors" data-node="${u.escapeHtml(node.name)}" data-action="enable">${t('common.retry')}</button>` : ''}
        ${isLoaded && node.tools?.length ? `<button class="node-run-btn btn btn-sm text-xs bg-ghost-600/20 text-ghost-400 hover:bg-ghost-600/40 transition-colors" data-node="${u.escapeHtml(node.name)}">${t('nodes.run')}</button>` : ''}
        ${node.source !== 'bundled' ? `<button class="node-delete-btn btn btn-sm text-xs bg-surface-700 text-zinc-300 hover:bg-red-500/20 hover:text-red-400 transition-colors ml-auto" data-node="${u.escapeHtml(node.name)}" data-has-models="${(m.models || []).length > 0}">${t('nodes.delete')}</button>` : ''}
      </div>
    </div>
  `;
}
