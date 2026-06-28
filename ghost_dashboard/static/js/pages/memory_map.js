/**
 * Memory Map — an interactive, dependency-free knowledge graph of Ghost's
 * long-term memory. Memories cluster around per-type hub nodes and are
 * cross-linked by shared tags, turning the flat memory list into a living
 * "mind" you can explore. Pure SVG + a small spring simulation (no libs), so
 * it works fully offline like the rest of Ghost.
 *
 * Data comes from GET /api/memory/graph (read-only over the existing store).
 */

const SVGNS = 'http://www.w3.org/2000/svg';

const TYPE_PALETTE = [
  '#a78bfa', '#22d3ee', '#34d399', '#f472b6', '#fbbf24', '#60a5fa',
  '#f87171', '#c084fc', '#4ade80', '#fb923c', '#38bdf8', '#e879f9',
];

function colorForTypes(types) {
  const keys = Object.keys(types).sort();
  const map = {};
  keys.forEach((k, i) => { map[k] = TYPE_PALETTE[i % TYPE_PALETTE.length]; });
  return map;
}

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;

  container.innerHTML = `
    <div class="mm-head flex items-center justify-between flex-wrap gap-2 mb-3">
      <div>
        <h1 class="page-header mb-0">Memory Map</h1>
        <p class="page-desc mb-0">Ghost's long-term memory as a living graph — clustered by type, linked by shared tags.</p>
      </div>
      <div class="flex items-center gap-2">
        <label class="text-xs text-zinc-500">Nodes</label>
        <select id="mm-limit" class="form-input text-xs py-1 w-20">
          <option value="120">120</option>
          <option value="180" selected>180</option>
          <option value="280">280</option>
          <option value="400">400</option>
        </select>
        <button id="mm-relayout" class="btn btn-ghost btn-sm" title="Re-run layout">↻ Relayout</button>
        <button id="mm-fit" class="btn btn-ghost btn-sm" title="Fit to view">⤢ Fit</button>
      </div>
    </div>

    <div id="mm-stats" class="flex flex-wrap gap-3 mb-3 text-xs text-zinc-500"></div>

    <div class="mm-wrap" style="position:relative;">
      <div id="mm-legend" class="mm-legend"></div>
      <svg id="mm-svg" class="mm-svg" xmlns="${SVGNS}"></svg>
      <div id="mm-tip" class="mm-tip" style="display:none;"></div>
      <div id="mm-detail" class="mm-detail" style="display:none;"></div>
      <div id="mm-empty" class="mm-empty" style="display:none;">
        <div class="text-3xl mb-2">🧠</div>
        <div class="text-zinc-400">No memories yet. As Ghost works and chats, its memory map will grow here.</div>
      </div>
    </div>
  `;

  const svg = container.querySelector('#mm-svg');
  const tip = container.querySelector('#mm-tip');
  const detail = container.querySelector('#mm-detail');
  const legendEl = container.querySelector('#mm-legend');
  const statsEl = container.querySelector('#mm-stats');
  const emptyEl = container.querySelector('#mm-empty');

  let sim = null;   // active simulation handle (so we can stop on rebuild)

  async function load() {
    const limit = container.querySelector('#mm-limit').value;
    statsEl.innerHTML = `<span class="mm-loading">Loading memory graph…</span>`;
    let data;
    try {
      data = await api.get(`/api/memory/graph?limit=${limit}`);
    } catch (err) {
      statsEl.innerHTML = `<span class="text-red-400">Failed to load: ${err?.message || err}</span>`;
      return;
    }
    if (sim) { sim.stop(); sim = null; }

    const memNodes = (data.nodes || []).filter((n) => n.kind === 'memory');
    if (!memNodes.length) {
      emptyEl.style.display = 'flex';
      svg.style.display = 'none';
      legendEl.style.display = 'none';
      statsEl.innerHTML = '';
      return;
    }
    emptyEl.style.display = 'none';
    svg.style.display = 'block';
    legendEl.style.display = 'flex';

    const colors = colorForTypes(data.types || {});
    statsEl.innerHTML = `
      <span><strong class="text-zinc-300">${data.shown}</strong> shown</span>
      <span><strong class="text-zinc-300">${data.total}</strong> total memories</span>
      <span><strong class="text-zinc-300">${Object.keys(data.types || {}).length}</strong> types</span>
      <span><strong class="text-zinc-300">${(data.links || []).filter((l) => l.kind === 'tag').length}</strong> tag links</span>
    `;

    renderLegend(legendEl, data.types || {}, colors);
    sim = buildGraph({ svg, tip, detail, data, colors, u });
  }

  // Controls
  container.querySelector('#mm-limit').addEventListener('change', load);
  container.querySelector('#mm-relayout').addEventListener('click', () => { if (sim) sim.relayout(); });
  container.querySelector('#mm-fit').addEventListener('click', () => { if (sim) sim.fit(); });

  await load();

  // Stop the animation loop when the page is torn down.
  const observer = new MutationObserver(() => {
    if (!document.body.contains(svg)) {
      if (sim) sim.stop();
      observer.disconnect();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

function renderLegend(el, types, colors) {
  const entries = Object.entries(types).sort((a, b) => b[1] - a[1]);
  el.innerHTML = entries.map(([ty, cnt]) => `
    <button class="mm-legend-item" data-type="${ty}">
      <span class="mm-dot" style="background:${colors[ty]}"></span>
      <span class="mm-legend-label">${ty}</span>
      <span class="mm-legend-count">${cnt}</span>
    </button>
  `).join('');
}

/* ───────────────────────── force-directed graph ───────────────────────── */

function buildGraph({ svg, tip, detail, data, colors, u }) {
  const wrap = svg.parentElement;
  let W = wrap.clientWidth || 800;
  let H = Math.max(460, Math.min(window.innerHeight - 240, 720));
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = '';

  // Layers (group for pan/zoom).
  const viewport = el('g', { class: 'mm-viewport' });
  const gLinks = el('g', { class: 'mm-links' });
  const gNodes = el('g', { class: 'mm-nodes' });
  viewport.appendChild(gLinks);
  viewport.appendChild(gNodes);
  svg.appendChild(viewport);

  // Build node + link models.
  const nodes = (data.nodes || []).map((n) => ({ ...n }));
  const byId = new Map();
  nodes.forEach((n) => byId.set(n.id, n));
  const links = (data.links || [])
    .map((l) => ({ ...l, source: byId.get(l.s), target: byId.get(l.t) }))
    .filter((l) => l.source && l.target);

  // Degree (for memory-node sizing).
  const degree = new Map();
  links.forEach((l) => {
    degree.set(l.source.id, (degree.get(l.source.id) || 0) + 1);
    degree.set(l.target.id, (degree.get(l.target.id) || 0) + 1);
  });

  // Initial positions: type hubs on a ring, memories scattered near center.
  const hubs = nodes.filter((n) => n.kind === 'type');
  hubs.forEach((n, i) => {
    const a = (i / Math.max(1, hubs.length)) * Math.PI * 2;
    n.x = Math.cos(a) * 140;
    n.y = Math.sin(a) * 140;
    n.mass = 3.2;
  });
  nodes.filter((n) => n.kind === 'memory').forEach((n) => {
    n.x = (Math.random() - 0.5) * 320;
    n.y = (Math.random() - 0.5) * 320;
    n.mass = 1;
  });
  nodes.forEach((n) => { n.vx = 0; n.vy = 0; });

  function radius(n) {
    if (n.kind === 'type') return 13 + Math.min(10, (n.count || 0) ** 0.28);
    return 3.5 + Math.min(6, (degree.get(n.id) || 0) * 0.7);
  }

  // SVG elements.
  const linkEls = links.map((l) => {
    const ln = el('line', {
      class: `mm-link mm-link-${l.kind}`,
      'stroke-width': l.kind === 'tag' ? Math.min(2.2, 0.7 + (l.w || 1) * 0.4) : 1,
    });
    gLinks.appendChild(ln);
    return ln;
  });

  const nodeEls = nodes.map((n) => {
    const g = el('g', { class: `mm-node mm-node-${n.kind}`, 'data-id': n.id });
    const c = el('circle', {
      r: radius(n),
      fill: colors[n.type] || '#9ca3af',
      'fill-opacity': n.kind === 'type' ? 0.92 : 0.85,
      stroke: n.kind === 'type' ? '#0a0a0a' : 'rgba(0,0,0,0.45)',
      'stroke-width': n.kind === 'type' ? 2 : 1,
    });
    g.appendChild(c);
    if (n.kind === 'type') {
      const tx = el('text', { class: 'mm-hub-label', x: 0, y: radius(n) + 12, 'text-anchor': 'middle' });
      tx.textContent = `${n.label} · ${n.count}`;
      g.appendChild(tx);
    }
    gNodes.appendChild(g);
    n._g = g; n._c = c;
    return g;
  });

  // Adjacency for hover highlight.
  const adj = new Map();
  nodes.forEach((n) => adj.set(n.id, new Set()));
  links.forEach((l) => { adj.get(l.source.id).add(l.target.id); adj.get(l.target.id).add(l.source.id); });

  /* ----- simulation ----- */
  let alpha = 1;
  let raf = null;
  let running = true;
  const N = nodes.length;

  function tick() {
    // Repulsion (all pairs — fine for a few hundred nodes).
    for (let i = 0; i < N; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < N; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = (Math.random() - 0.5); dy = (Math.random() - 0.5); d2 = 0.01; }
        const d = Math.sqrt(d2);
        const rep = 2600 / d2;
        const fx = (dx / d) * rep, fy = (dy / d) * rep;
        a.vx += fx / a.mass; a.vy += fy / a.mass;
        b.vx -= fx / b.mass; b.vy -= fy / b.mass;
      }
    }
    // Springs (links).
    for (const l of links) {
      const a = l.source, b = l.target;
      const target = l.kind === 'type' ? 64 : 120;
      const k = l.kind === 'type' ? 0.045 : 0.018;
      let dx = b.x - a.x, dy = b.y - a.y;
      let d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d - target) * k;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx / a.mass; a.vy += fy / a.mass;
      b.vx -= fx / b.mass; b.vy -= fy / b.mass;
    }
    // Gravity toward center + integrate.
    for (const n of nodes) {
      if (n._pinned) { n.vx = 0; n.vy = 0; continue; }
      n.vx += -n.x * 0.004;
      n.vy += -n.y * 0.004;
      n.vx *= 0.86; n.vy *= 0.86;
      n.x += n.vx * alpha;
      n.y += n.vy * alpha;
    }
    draw();
    alpha *= 0.992;
    if (alpha < 0.004) running = false;
  }

  function draw() {
    for (let i = 0; i < links.length; i++) {
      const l = links[i], e = linkEls[i];
      e.setAttribute('x1', l.source.x); e.setAttribute('y1', l.source.y);
      e.setAttribute('x2', l.target.x); e.setAttribute('y2', l.target.y);
    }
    for (const n of nodes) n._g.setAttribute('transform', `translate(${n.x},${n.y})`);
  }

  function loop() {
    if (running) tick();
    raf = requestAnimationFrame(loop);
  }
  loop();

  /* ----- view transform (pan / zoom) ----- */
  const view = { x: W / 2, y: H / 2, k: 1 };
  function applyView() {
    viewport.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.k})`);
  }
  applyView();

  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const rect = svg.getBoundingClientRect();
    const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
    const nk = Math.max(0.25, Math.min(4, view.k * factor));
    // zoom toward cursor
    view.x = mx - (mx - view.x) * (nk / view.k);
    view.y = my - (my - view.y) * (nk / view.k);
    view.k = nk;
    applyView();
  }, { passive: false });

  // Drag: node (pin/move) or background (pan).
  let drag = null;
  function screenToWorld(ev) {
    const rect = svg.getBoundingClientRect();
    return {
      x: (ev.clientX - rect.left - view.x) / view.k,
      y: (ev.clientY - rect.top - view.y) / view.k,
    };
  }
  svg.addEventListener('mousedown', (ev) => {
    const g = ev.target.closest('.mm-node');
    if (g) {
      const n = byId.get(g.dataset.id);
      drag = { type: 'node', n };
      n._pinned = true;
      alpha = Math.max(alpha, 0.35); running = true;
    } else {
      drag = { type: 'pan', x0: ev.clientX, y0: ev.clientY, vx: view.x, vy: view.y };
    }
  });
  window.addEventListener('mousemove', (ev) => {
    if (!drag) return;
    if (drag.type === 'node') {
      const w = screenToWorld(ev);
      drag.n.x = w.x; drag.n.y = w.y; drag.n.vx = 0; drag.n.vy = 0;
      draw();
    } else {
      view.x = drag.vx + (ev.clientX - drag.x0);
      view.y = drag.vy + (ev.clientY - drag.y0);
      applyView();
    }
  });
  window.addEventListener('mouseup', () => {
    if (drag && drag.type === 'node' && drag.n) drag.n._pinned = false;
    drag = null;
  });

  /* ----- hover + click ----- */
  function setHighlight(id) {
    if (!id) {
      svg.classList.remove('mm-hovering');
      nodeEls.forEach((g) => g.classList.remove('mm-dim', 'mm-hot'));
      linkEls.forEach((e) => e.classList.remove('mm-dim', 'mm-hot'));
      return;
    }
    svg.classList.add('mm-hovering');
    const neighbors = adj.get(id) || new Set();
    nodes.forEach((n) => {
      const hot = n.id === id || neighbors.has(n.id);
      n._g.classList.toggle('mm-hot', hot);
      n._g.classList.toggle('mm-dim', !hot);
    });
    links.forEach((l, i) => {
      const hot = l.source.id === id || l.target.id === id;
      linkEls[i].classList.toggle('mm-hot', hot);
      linkEls[i].classList.toggle('mm-dim', !hot);
    });
  }

  gNodes.addEventListener('mouseover', (ev) => {
    const g = ev.target.closest('.mm-node');
    if (!g) return;
    const n = byId.get(g.dataset.id);
    setHighlight(n.id);
    tip.style.display = 'block';
    tip.innerHTML = n.kind === 'type'
      ? `<div class="mm-tip-type">${n.label}</div><div class="mm-tip-sub">${n.count} memories of this type</div>`
      : `<div class="mm-tip-type" style="color:${colors[n.type]}">${n.type}</div>
         <div class="mm-tip-body">${u.escapeHtml(n.label)}</div>
         ${n.tags?.length ? `<div class="mm-tip-tags">${n.tags.map((t) => `#${u.escapeHtml(t)}`).join(' ')}</div>` : ''}`;
  });
  gNodes.addEventListener('mousemove', (ev) => {
    const rect = wrap.getBoundingClientRect();
    let x = ev.clientX - rect.left + 14, y = ev.clientY - rect.top + 14;
    if (x + 240 > rect.width) x = rect.width - 248;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  });
  gNodes.addEventListener('mouseout', (ev) => {
    if (ev.relatedTarget && ev.relatedTarget.closest && ev.relatedTarget.closest('.mm-node')) return;
    setHighlight(null);
    tip.style.display = 'none';
  });

  gNodes.addEventListener('click', (ev) => {
    const g = ev.target.closest('.mm-node');
    if (!g) return;
    const n = byId.get(g.dataset.id);
    if (n.kind === 'type') return;
    showDetail(n);
  });

  function showDetail(n) {
    detail.style.display = 'block';
    detail.innerHTML = `
      <button class="mm-detail-close" title="Close">✕</button>
      <div class="mm-detail-type" style="color:${colors[n.type]}">${n.type}</div>
      <div class="mm-detail-meta">${n.ts || ''}${n.tokens ? ` · ${n.tokens} tokens` : ''}${n.skill ? ` · ${u.escapeHtml(n.skill)}` : ''}</div>
      <div class="mm-detail-body">${u.escapeHtml(n.preview || n.label)}</div>
      ${n.tags?.length ? `<div class="mm-detail-tags">${n.tags.map((t) => `<span class="mm-tag">#${u.escapeHtml(t)}</span>`).join('')}</div>` : ''}
    `;
    detail.querySelector('.mm-detail-close').addEventListener('click', () => { detail.style.display = 'none'; });
  }

  // Legend interactions: hover a type to highlight its cluster.
  const legendEl = wrap.querySelector('#mm-legend');
  legendEl?.addEventListener('mouseover', (ev) => {
    const btn = ev.target.closest('.mm-legend-item');
    if (!btn) return;
    const ty = btn.dataset.type;
    nodes.forEach((n) => {
      const hot = n.type === ty;
      n._g.classList.toggle('mm-hot', hot);
      n._g.classList.toggle('mm-dim', !hot);
    });
    linkEls.forEach((e) => e.classList.add('mm-dim'));
    svg.classList.add('mm-hovering');
  });
  legendEl?.addEventListener('mouseout', () => setHighlight(null));

  /* ----- fit-to-view ----- */
  function fit() {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
      minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
    }
    const w = (maxX - minX) || 1, h = (maxY - minY) || 1;
    const k = Math.max(0.25, Math.min(2, 0.85 * Math.min(W / w, H / h)));
    view.k = k;
    view.x = W / 2 - ((minX + maxX) / 2) * k;
    view.y = H / 2 - ((minY + maxY) / 2) * k;
    applyView();
  }
  setTimeout(fit, 900);

  function relayout() {
    nodes.forEach((n) => {
      if (n.kind === 'memory') { n.x = (Math.random() - 0.5) * 320; n.y = (Math.random() - 0.5) * 320; }
      n.vx = 0; n.vy = 0; n._pinned = false;
    });
    alpha = 1; running = true;
    setTimeout(fit, 900);
  }

  return {
    stop() { running = false; if (raf) cancelAnimationFrame(raf); },
    fit,
    relayout,
  };
}

function el(name, attrs) {
  const e = document.createElementNS(SVGNS, name);
  if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}
