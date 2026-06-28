/** Audit Log Page — View and filter security audit logs */

import { toast } from '../utils.js';

let currentFilters = {
  action: '',
  resource_type: '',
  success: '',
  since: '',
  until: '',
  limit: 100,
  offset: 0
};

let autoRefreshInterval = null;

export async function render(container) {
  container.innerHTML = `
    <div class="p-6">
      <div class="page-header mb-2">Audit Log</div>
      <div class="page-desc mb-6">Security audit trail for sensitive operations</div>

      <!-- Stats Cards -->
      <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6" id="audit-stats">
        <div class="stat-card"><div class="stat-value" id="stat-total">-</div><div class="stat-label">Total Entries</div></div>
        <div class="stat-card"><div class="stat-value" id="stat-today">-</div><div class="stat-label">Today</div></div>
        <div class="stat-card"><div class="stat-value text-emerald-400" id="stat-success">-</div><div class="stat-label">Successful</div></div>
        <div class="stat-card"><div class="stat-value text-red-400" id="stat-failed">-</div><div class="stat-label">Failed</div></div>
      </div>

      <!-- Filters -->
      <div class="stat-card mb-4">
        <div class="flex flex-wrap gap-3 items-end">
          <div>
            <label class="form-label">Action</label>
            <select id="filter-action" class="form-input text-sm">
              <option value="">All Actions</option>
            </select>
          </div>
          <div>
            <label class="form-label">Resource Type</label>
            <select id="filter-resource" class="form-input text-sm">
              <option value="">All Types</option>
              <option value="config">Config</option>
              <option value="credential">Credential</option>
              <option value="extension">Extension</option>
              <option value="auth_profile">Auth Profile</option>
            </select>
          </div>
          <div>
            <label class="form-label">Status</label>
            <select id="filter-success" class="form-input text-sm">
              <option value="">All</option>
              <option value="true">Success</option>
              <option value="false">Failed</option>
            </select>
          </div>
          <div>
            <label class="form-label">Since</label>
            <input type="datetime-local" id="filter-since" class="form-input text-sm" />
          </div>
          <div>
            <label class="form-label">Until</label>
            <input type="datetime-local" id="filter-until" class="form-input text-sm" />
          </div>
          <div class="flex gap-2">
            <button id="btn-apply" class="btn btn-primary btn-sm">Apply Filters</button>
            <button id="btn-reset" class="btn btn-secondary btn-sm">Reset</button>
          </div>
        </div>
        <div class="flex items-center gap-3 mt-3 pt-3 border-t border-surface-600/30">
          <label class="flex items-center gap-2 text-sm text-zinc-400 cursor-pointer">
            <input type="checkbox" id="auto-refresh" class="rounded bg-surface-800 border-surface-600" />
            Auto-refresh (5s)
          </label>
          <button id="btn-export" class="btn btn-ghost btn-sm ml-auto">Export JSON</button>
        </div>
      </div>

      <!-- Entries Table -->
      <div class="stat-card">
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-left text-zinc-500 border-b border-surface-600/30">
                <th class="pb-2 font-medium">Timestamp</th>
                <th class="pb-2 font-medium">Action</th>
                <th class="pb-2 font-medium">Resource</th>
                <th class="pb-2 font-medium">Actor</th>
                <th class="pb-2 font-medium">Status</th>
                <th class="pb-2 font-medium">Details</th>
              </tr>
            </thead>
            <tbody id="audit-entries" class="text-zinc-300">
              <tr><td colspan="6" class="py-8 text-center text-zinc-500">Loading...</td></tr>
            </tbody>
          </table>
        </div>
        
        <!-- Pagination -->
        <div class="flex items-center justify-between mt-4 pt-4 border-t border-surface-600/30">
          <div class="text-sm text-zinc-500">
            Showing <span id="page-start">0</span>-<span id="page-end">0</span> entries
          </div>
          <div class="flex gap-2">
            <button id="btn-prev" class="btn btn-secondary btn-sm" disabled>Previous</button>
            <button id="btn-next" class="btn btn-secondary btn-sm" disabled>Next</button>
          </div>
        </div>
      </div>
    </div>
  `;

  // Load action types
  loadActionTypes();
  
  // Load initial data
  loadEntries();
  loadStats();

  // Event listeners
  container.querySelector('#btn-apply').addEventListener('click', () => {
    currentFilters.offset = 0;
    updateFiltersFromUI();
    loadEntries();
  });

  container.querySelector('#btn-reset').addEventListener('click', () => {
    container.querySelector('#filter-action').value = '';
    container.querySelector('#filter-resource').value = '';
    container.querySelector('#filter-success').value = '';
    container.querySelector('#filter-since').value = '';
    container.querySelector('#filter-until').value = '';
    currentFilters = { action: '', resource_type: '', success: '', since: '', until: '', limit: 100, offset: 0 };
    loadEntries();
  });

  container.querySelector('#btn-prev').addEventListener('click', () => {
    currentFilters.offset = Math.max(0, currentFilters.offset - currentFilters.limit);
    loadEntries();
  });

  container.querySelector('#btn-next').addEventListener('click', () => {
    currentFilters.offset += currentFilters.limit;
    loadEntries();
  });

  container.querySelector('#btn-export').addEventListener('click', exportEntries);

  container.querySelector('#auto-refresh').addEventListener('change', (e) => {
    if (e.target.checked) {
      autoRefreshInterval = setInterval(() => {
        loadEntries();
        loadStats();
      }, 5000);
    } else {
      clearInterval(autoRefreshInterval);
      autoRefreshInterval = null;
    }
  });

  // Cleanup on page change
  return () => {
    if (autoRefreshInterval) {
      clearInterval(autoRefreshInterval);
    }
  };
}

function updateFiltersFromUI() {
  currentFilters.action = document.getElementById('filter-action').value;
  currentFilters.resource_type = document.getElementById('filter-resource').value;
  currentFilters.success = document.getElementById('filter-success').value;
  currentFilters.since = document.getElementById('filter-since').value;
  currentFilters.until = document.getElementById('filter-until').value;
}

async function loadActionTypes() {
  try {
    const data = await window.GhostAPI.get('/api/audit/actions');
    const select = document.getElementById('filter-action');
    const categories = {};
    
    data.actions.forEach(action => {
      const cat = action.category || 'other';
      if (!categories[cat]) categories[cat] = [];
      categories[cat].push(action.value);
    });
    
    Object.entries(categories).sort().forEach(([cat, actions]) => {
      const optgroup = document.createElement('optgroup');
      optgroup.label = cat.charAt(0).toUpperCase() + cat.slice(1);
      actions.sort().forEach(action => {
        const option = document.createElement('option');
        option.value = action;
        option.textContent = action;
        optgroup.appendChild(option);
      });
      select.appendChild(optgroup);
    });
  } catch (err) {
    console.error('Failed to load action types:', err);
  }
}

async function loadEntries() {
  try {
    const params = new URLSearchParams();
    if (currentFilters.action) params.set('action', currentFilters.action);
    if (currentFilters.resource_type) params.set('resource_type', currentFilters.resource_type);
    if (currentFilters.success) params.set('success', currentFilters.success);
    if (currentFilters.since) params.set('since', currentFilters.since);
    if (currentFilters.until) params.set('until', currentFilters.until);
    params.set('limit', currentFilters.limit);
    params.set('offset', currentFilters.offset);

    const data = await window.GhostAPI.get(`/api/audit?${params}`);
    renderEntries(data.entries);
    
    // Update pagination
    const start = currentFilters.offset + 1;
    const end = currentFilters.offset + data.entries.length;
    document.getElementById('page-start').textContent = data.entries.length ? start : 0;
    document.getElementById('page-end').textContent = end;
    document.getElementById('btn-prev').disabled = currentFilters.offset === 0;
    document.getElementById('btn-next').disabled = data.entries.length < currentFilters.limit;
  } catch (err) {
    console.error('Failed to load audit entries:', err);
    document.getElementById('audit-entries').innerHTML = 
      `<tr><td colspan="6" class="py-8 text-center text-red-400">Error: ${err.message}</td></tr>`;
  }
}

function renderEntries(entries) {
  const tbody = document.getElementById('audit-entries');
  
  if (!entries.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="py-8 text-center text-zinc-500">Nothing logged for this view \u2014 a quiet audit trail is a healthy one.</td></tr>`;
    return;
  }

  tbody.innerHTML = entries.map(entry => {
    const timestamp = new Date(entry.timestamp).toLocaleString();
    const actionClass = entry.action?.split('.')[0] || 'unknown';
    const successBadge = entry.success 
      ? '<span class="badge badge-green">Success</span>'
      : '<span class="badge badge-red">Failed</span>';
    const actor = entry.actor ? (entry.actor.id || entry.actor.ip || 'system') : 'system';
    const details = entry.details ? JSON.stringify(entry.details).slice(0, 100) + '...' : '-';
    const error = entry.error ? `<div class="text-red-400 text-xs mt-1">${escapeHtml(entry.error)}</div>` : '';
    
    return `
      <tr class="border-b border-surface-600/20 hover:bg-surface-700/30">
        <td class="py-3 font-mono text-xs text-zinc-400">${timestamp}</td>
        <td class="py-3">
          <span class="text-ghost-400">${escapeHtml(entry.action)}</span>
        </td>
        <td class="py-3">
          <span class="text-zinc-400">${escapeHtml(entry.resource_type)}</span>
          <div class="text-xs text-zinc-500">${escapeHtml(entry.resource_id || '-')}</div>
        </td>
        <td class="py-3 text-zinc-400">${escapeHtml(actor)}</td>
        <td class="py-3">${successBadge}</td>
        <td class="py-3">
          <div class="text-xs text-zinc-500 max-w-xs truncate" title="${escapeHtml(details)}">${escapeHtml(details)}</div>
          ${error}
        </td>
      </tr>
    `;
  }).join('');
}

async function loadStats() {
  try {
    const stats = await window.GhostAPI.get('/api/audit/stats');
    document.getElementById('stat-total').textContent = stats.total_entries?.toLocaleString() || 0;
    document.getElementById('stat-today').textContent = stats.today_entries?.toLocaleString() || 0;
    document.getElementById('stat-success').textContent = stats.successful_entries?.toLocaleString() || 0;
    document.getElementById('stat-failed').textContent = stats.failed_entries?.toLocaleString() || 0;
  } catch (err) {
    console.error('Failed to load stats:', err);
  }
}

async function exportEntries() {
  try {
    const data = await window.GhostAPI.post('/api/audit/export', {
      since: currentFilters.since,
      until: currentFilters.until
    });
    
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `audit-export-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    toast.success(`Exported ${data.count} entries`);
  } catch (err) {
    toast.error('Export failed: ' + err.message);
  }
}

function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
