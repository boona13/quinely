/** MCP Servers page — manage external Model Context Protocol tool servers. */

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;

  let data;
  try {
    data = await api.get('/api/mcp/status');
  } catch (e) {
    container.innerHTML = `<h1 class="page-header">MCP Servers</h1>
      <div class="stat-card"><p class="text-zinc-500 text-sm">MCP API not available.</p></div>`;
    return;
  }

  const esc = (s) => u.escapeHtml(String(s ?? ''));
  const servers = data.servers || [];

  const badge = (ok, label, color) =>
    `<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-${color}-500/20 text-${color}-400 font-medium">${label}</span>`;

  const serverCard = (s) => {
    const argStr = (s.args || []).join(' ');
    let state;
    if (s.disabled) state = badge(false, 'DISABLED', 'zinc');
    else if (s.connected) state = badge(true, `CONNECTED · ${s.tool_count} tools`, 'emerald');
    else if (s.error) state = badge(false, 'ERROR', 'red');
    else state = badge(false, 'NOT CONNECTED', 'amber');

    const tools = (s.tools || []).length
      ? `<div class="flex flex-wrap gap-1 mt-2">${s.tools.map(t =>
          `<span class="text-[9px] px-1.5 py-0.5 rounded bg-zinc-700/60 text-zinc-300 font-mono">${esc(t)}</span>`).join('')}</div>`
      : '';
    const err = s.error
      ? `<div class="text-[10px] text-red-400 mt-2 font-mono break-all">${esc(s.error)}</div>` : '';

    return `<div class="stat-card mb-3" data-server="${esc(s.name)}">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="text-sm font-semibold text-white">${esc(s.name)}</span>
            ${state}
          </div>
          <div class="text-[11px] text-zinc-400 mt-1 font-mono break-all">${esc(s.command)} ${esc(argStr)}</div>
          ${tools}
          ${err}
        </div>
        <div class="flex flex-col gap-1.5 flex-shrink-0">
          <button class="mcp-edit text-[10px] px-2.5 py-1 rounded bg-zinc-700/60 text-zinc-200 hover:bg-zinc-600" data-name="${esc(s.name)}">Edit</button>
          <button class="mcp-toggle text-[10px] px-2.5 py-1 rounded bg-zinc-700/60 text-zinc-200 hover:bg-zinc-600" data-name="${esc(s.name)}">${s.disabled ? 'Enable' : 'Disable'}</button>
          <button class="mcp-delete text-[10px] px-2.5 py-1 rounded bg-red-500/15 text-red-400 hover:bg-red-500/25" data-name="${esc(s.name)}">Delete</button>
        </div>
      </div>
    </div>`;
  };

  const disabledNotice = !data.enabled ? `
    <div class="stat-card mb-4 border border-amber-500/30">
      <div class="text-sm font-medium text-amber-400">MCP is disabled</div>
      <p class="text-xs text-zinc-400 mt-1">Enable <span class="font-mono">enable_mcp</span> in
      Configuration → Feature Toggles, then restart Ghost to connect servers.</p>
    </div>` : '';

  container.innerHTML = `
    <h1 class="page-header">MCP Servers</h1>
    <p class="page-desc">Connect external Model Context Protocol tool servers. Their tools are bridged into Ghost as <span class="font-mono">mcp_&lt;server&gt;_&lt;tool&gt;</span>.</p>

    ${disabledNotice}

    <div class="flex items-center justify-between mb-4">
      <div class="text-[11px] text-zinc-500 font-mono break-all">Config: ${esc(data.config_path)}</div>
      <button id="mcp-reload" class="text-xs px-3 py-1.5 rounded bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30 font-medium">Reconnect all</button>
    </div>
    <div id="mcp-reload-result" class="text-xs mb-4 hidden"></div>

    <div class="stat-card mb-6">
      <h3 class="text-sm font-semibold text-white mb-3" id="mcp-form-title">Add a server</h3>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label class="form-label">Name</label>
          <input id="mcp-name" class="form-input w-full" placeholder="filesystem">
        </div>
        <div>
          <label class="form-label">Command</label>
          <input id="mcp-command" class="form-input w-full" placeholder="npx">
        </div>
      </div>
      <div class="mt-3">
        <label class="form-label">Arguments (space-separated)</label>
        <input id="mcp-args" class="form-input w-full" placeholder="-y @modelcontextprotocol/server-filesystem /path/to/allow">
      </div>
      <div class="mt-3">
        <label class="form-label">Environment (KEY=VALUE per line, optional)</label>
        <textarea id="mcp-env" class="form-input w-full font-mono text-xs" rows="2" placeholder="API_KEY=..."></textarea>
      </div>
      <div class="flex gap-2 mt-3">
        <button id="mcp-save" class="text-xs px-4 py-1.5 rounded bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 font-medium">Save server</button>
        <button id="mcp-clear" class="text-xs px-4 py-1.5 rounded bg-zinc-700/60 text-zinc-300 hover:bg-zinc-600 hidden">Cancel edit</button>
      </div>
      <div id="mcp-save-result" class="text-xs mt-2 hidden"></div>
    </div>

    <h3 class="text-sm font-semibold text-white mb-3">Configured servers (${servers.length})</h3>
    <div id="mcp-server-list">
      ${servers.length ? servers.map(serverCard).join('')
        : '<div class="stat-card"><p class="text-zinc-500 text-sm">No servers configured yet. Add one above.</p></div>'}
    </div>
  `;

  const $ = (id) => document.getElementById(id);
  const showResult = (el, msg, ok) => {
    el.className = `text-xs mt-2 ${ok ? 'text-emerald-400' : 'text-red-400'}`;
    el.textContent = msg;
    el.classList.remove('hidden');
  };

  const reload = () => render(container);

  $('mcp-reload').addEventListener('click', async () => {
    const out = $('mcp-reload-result');
    out.className = 'text-xs mb-4 text-zinc-400';
    out.textContent = 'Reconnecting…';
    out.classList.remove('hidden');
    try {
      const r = await api.post('/api/mcp/reload', {});
      if (r.error) { showResult(out, r.error, false); return; }
      out.className = 'text-xs mb-4 text-emerald-400';
      out.textContent = `Connected ${r.connected} server(s), bridged ${r.tools_bridged} tool(s).`;
      setTimeout(reload, 900);
    } catch (e) { showResult(out, 'Reload failed: ' + e, false); }
  });

  const collectForm = () => {
    const env = {};
    ($('mcp-env').value || '').split('\n').forEach(line => {
      const i = line.indexOf('=');
      if (i > 0) env[line.slice(0, i).trim()] = line.slice(i + 1).trim();
    });
    return {
      name: $('mcp-name').value.trim(),
      command: $('mcp-command').value.trim(),
      args: $('mcp-args').value.trim(),
      env,
    };
  };

  $('mcp-save').addEventListener('click', async () => {
    const out = $('mcp-save-result');
    const body = collectForm();
    if (!body.name || !body.command) { showResult(out, 'Name and command are required.', false); return; }
    try {
      const r = await api.post('/api/mcp/servers', body);
      if (r.error) { showResult(out, r.error, false); return; }
      showResult(out, r.message || 'Saved.', true);
      setTimeout(reload, 700);
    } catch (e) { showResult(out, 'Save failed: ' + e, false); }
  });

  $('mcp-clear').addEventListener('click', reload);

  container.querySelectorAll('.mcp-edit').forEach(btn => btn.addEventListener('click', () => {
    const s = servers.find(x => x.name === btn.dataset.name);
    if (!s) return;
    $('mcp-form-title').textContent = `Edit "${s.name}"`;
    $('mcp-name').value = s.name;
    $('mcp-command').value = s.command;
    $('mcp-args').value = (s.args || []).join(' ');
    $('mcp-env').value = Object.entries(s.env || {}).map(([k, v]) => `${k}=${v}`).join('\n');
    $('mcp-clear').classList.remove('hidden');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }));

  container.querySelectorAll('.mcp-toggle').forEach(btn => btn.addEventListener('click', async () => {
    try { await api.post(`/api/mcp/servers/${encodeURIComponent(btn.dataset.name)}/toggle`, {}); reload(); }
    catch (e) { /* noop */ }
  }));

  container.querySelectorAll('.mcp-delete').forEach(btn => btn.addEventListener('click', async () => {
    if (!confirm(`Delete MCP server "${btn.dataset.name}"?`)) return;
    try { await api.post(`/api/mcp/servers/${encodeURIComponent(btn.dataset.name)}/delete`, {}); reload(); }
    catch (e) { /* noop */ }
  }));
}
