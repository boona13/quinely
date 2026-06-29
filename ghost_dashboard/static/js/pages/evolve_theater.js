/** Quinely Dashboard — Self-Evolution Theater
 *
 * Cinematic replay of Quinely's self-evolution cycles.
 * Designed for screen recording and viral demos.
 */

const api = window.GhostAPI;

/* ── State ──────────────────────────────────────────────────── */
let _history = [];
let _stats = {};
let _playing = false;
let _speed = 1;
let _timers = [];
let _animId = null;
let _evoIdx = -1;

/* ── ECG Heartbeat ──────────────────────────────────────────── */
let _canvas, _ctx;
let _gridImg;
let _ecgMode = 'idle';

const _ECG_CAP = 2048;
const _ecgBuf = new Float32Array(_ECG_CAP);
let _ecgHead = 0;
let _ecgLen = 0;
let _ecgT = 0;
let _ecgAmp = 0.6;
let _ecgPrevMode = '';
let _ecgLastTs = 0;
let _ecgW = 0;
let _ecgH = 0;
let _ecgMid = 0;
let _ecgScale = 0;

const _BEAT = [
  0,0,0,0,0,0,0,0,0,0,
  .06,.12,.06,0,
  -.06, .85, -.18, 0,
  0,0, .1,.18,.12,0,
  0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
];
const _FLAT = Array(_BEAT.length).fill(0);
const _SPIKE = [
  0,0,.08,.25,.5,1,.85,.55,.25,.08,
  -.06,0,0,0,0,0,0,0,0,0,
  0,0,0,0,0,0,0,0,0,0,
];

const _ECG = {
  idle:     { pat: _BEAT, pps: 22, aMin: 0.4,  aMax: 0.65 },
  active:   { pat: _BEAT, pps: 32, aMin: 0.7,  aMax: 0.95 },
  elevated: { pat: _BEAT, pps: 44, aMin: 0.9,  aMax: 1.2  },
  spike:    { pat: _SPIKE,pps: 36, aMin: 1.0,  aMax: 1.0  },
  flatline: { pat: _FLAT, pps: 30, aMin: 0.0,  aMax: 0.0  },
};

function _ecgSample(c) {
  const pat = c.pat;
  const len = pat.length;
  const prevCycle = Math.floor(_ecgT / len);
  _ecgT += c.pps / 60;
  if (Math.floor(_ecgT / len) !== prevCycle) {
    _ecgAmp = c.aMin + Math.random() * (c.aMax - c.aMin);
  }
  const t = _ecgT % len;
  const i = Math.floor(t);
  const f = t - i;
  return (pat[i] + (pat[(i + 1) % len] - pat[i]) * f) * _ecgAmp;
}

function _ecgPush(v) {
  _ecgBuf[_ecgHead] = v;
  _ecgHead = (_ecgHead + 1) % _ECG_CAP;
  if (_ecgLen < _ECG_CAP) _ecgLen++;
}

function _ecgAt(i) {
  return _ecgBuf[(_ecgHead - _ecgLen + i + _ECG_CAP) % _ECG_CAP];
}

function _initCanvas(container) {
  _canvas = container.querySelector('#theater-ecg');
  if (!_canvas) return;
  const parent = _canvas.parentElement;
  const r = parent.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  _ecgW = Math.round(r.width);
  _ecgH = Math.round(r.height);
  _ecgMid = _ecgH / 2;
  _ecgScale = _ecgH * 0.36;
  _canvas.width = _ecgW * dpr;
  _canvas.height = _ecgH * dpr;
  _ctx = _canvas.getContext('2d');
  _ctx.scale(dpr, dpr);
  _canvas.style.width = _ecgW + 'px';
  _canvas.style.height = _ecgH + 'px';
  _ecgHead = 0;
  _ecgLen = 0;
  _ecgT = 0;
  _ecgLastTs = 0;
  _ecgPrevMode = '';

  const gc = document.createElement('canvas');
  gc.width = _canvas.width;
  gc.height = _canvas.height;
  const g = gc.getContext('2d');
  g.scale(dpr, dpr);
  g.strokeStyle = 'rgba(16,185,129,0.035)';
  g.lineWidth = 0.5;
  g.beginPath();
  for (let y = 0; y < _ecgH; y += 8) { g.moveTo(0, y); g.lineTo(_ecgW, y); }
  for (let x = 0; x < _ecgW; x += 8) { g.moveTo(x, 0); g.lineTo(x, _ecgH); }
  g.stroke();
  _gridImg = gc;
}

function _drawEcg(ts) {
  if (!_canvas || !document.body.contains(_canvas)) { _animId = null; return; }

  if (!_ecgLastTs) _ecgLastTs = ts;
  const dt = Math.min(ts - _ecgLastTs, 50);
  _ecgLastTs = ts;

  const c = _ECG[_ecgMode] || _ECG.active;
  const newPts = Math.max(1, Math.round(c.pps * dt / 1000));
  for (let n = 0; n < newPts; n++) _ecgPush(_ecgSample(c));

  const w = _ecgW;
  const h = _ecgH;
  const mid = _ecgMid;
  const amp = _ecgScale;
  const len = Math.min(_ecgLen, w);

  _ctx.clearRect(0, 0, _canvas.width, _canvas.height);
  if (_gridImg) _ctx.drawImage(_gridImg, 0, 0);

  const flat = _ecgMode === 'flatline';
  const startX = w - len;

  // single glow + line pass using composite layers
  _ctx.lineJoin = 'round';
  _ctx.lineCap = 'round';

  // glow (wide, soft)
  _ctx.beginPath();
  _ctx.strokeStyle = flat ? 'rgba(239,68,68,0.1)' : 'rgba(16,185,129,0.08)';
  _ctx.lineWidth = 7;
  for (let i = 0; i < len; i++) {
    const x = startX + i;
    const y = mid - _ecgAt(_ecgLen - len + i) * amp;
    i === 0 ? _ctx.moveTo(x, y) : _ctx.lineTo(x, y);
  }
  _ctx.stroke();

  // main line (crisp)
  const color = flat ? '#ef4444' : '#10b981';
  _ctx.beginPath();
  _ctx.strokeStyle = color;
  _ctx.lineWidth = 1.5;
  for (let i = 0; i < len; i++) {
    const x = startX + i;
    const y = mid - _ecgAt(_ecgLen - len + i) * amp;
    i === 0 ? _ctx.moveTo(x, y) : _ctx.lineTo(x, y);
  }
  _ctx.stroke();

  // leading dot
  if (len > 1) {
    const ly = mid - _ecgAt(_ecgLen - 1) * amp;
    _ctx.beginPath();
    _ctx.fillStyle = color;
    _ctx.shadowColor = color;
    _ctx.shadowBlur = 12;
    _ctx.arc(w, ly, 2.5, 0, Math.PI * 2);
    _ctx.fill();
    _ctx.shadowBlur = 0;
  }

  // border glow (only on mode change)
  if (_ecgMode !== _ecgPrevMode) {
    _ecgPrevMode = _ecgMode;
    const wrap = _canvas.closest('.theater-wrap');
    if (wrap) {
      wrap.classList.toggle('theater-danger', flat);
      wrap.classList.toggle('theater-pulse', _ecgMode === 'spike' || _ecgMode === 'elevated');
    }
  }

  _animId = requestAnimationFrame(_drawEcg);
}

/* ── Counter Animation ──────────────────────────────────────── */
function _rollCounter(el, target, ms = 2200) {
  if (!el || target === 0) { if (el) el.textContent = '0'; return; }
  const start = performance.now();
  (function tick(now) {
    const p = Math.min((now - start) / ms, 1);
    const eased = 1 - Math.pow(1 - p, 4);
    el.textContent = Math.round(target * eased).toLocaleString();
    if (p < 1) requestAnimationFrame(tick);
  })(start);
}

/* ── Terminal ───────────────────────────────────────────────── */
function _termLine(term, text, cls) {
  if (!term) return;
  const cur = term.querySelector('.theater-cursor');
  if (cur) cur.remove();
  const div = document.createElement('div');
  div.className = 'theater-line' + (cls ? ' ' + cls : '');
  div.textContent = text;
  term.appendChild(div);
  term.scrollTop = term.scrollHeight;
}

function _termCursor(term) {
  if (!term) return;
  const cur = term.querySelector('.theater-cursor');
  if (cur) return;
  const span = document.createElement('span');
  span.className = 'theater-cursor';
  term.appendChild(span);
}

/* ── Diff Renderer ──────────────────────────────────────────── */
function _esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _renderDiff(surg, diffText) {
  if (!surg || !diffText) return;
  surg.innerHTML = '';
  const lines = diffText.split('\n');
  const perLine = Math.max(15, Math.min(80, 1500 / Math.max(lines.length, 1)));
  let ln = 0;

  lines.forEach((line, i) => {
    const div = document.createElement('div');
    div.className = 'theater-diff-line';
    div.style.animationDelay = (i * perLine) + 'ms';

    let cls = 'ctx';
    if (line.startsWith('+++') || line.startsWith('---')) {
      cls = 'header';
    } else if (line.startsWith('@@')) {
      cls = 'hunk';
      const m = line.match(/@@ -\d+(?:,\d+)? \+(\d+)/);
      if (m) ln = parseInt(m[1]) - 1;
    } else if (line.startsWith('+')) {
      cls = 'add'; ln++;
    } else if (line.startsWith('-')) {
      cls = 'del';
    } else {
      ln++;
    }
    div.classList.add(cls);

    if (cls !== 'header' && cls !== 'hunk') {
      const num = cls === 'del' ? '  ' : String(ln).padStart(3);
      div.innerHTML = `<span class="theater-diff-num">${num}</span>${_esc(line)}`;
    } else {
      div.textContent = line;
    }
    surg.appendChild(div);
  });
}

/* ── Flash Effect ───────────────────────────────────────────── */
const _FLASH_TEXT = {
  deployed:    'DEPLOYED',
  'rolled-back': 'ROLLED BACK',
  'tests-passed': 'TESTS PASSED',
  'pr-approved': 'PR APPROVED',
  restarted:   'SYSTEM RESTARTED',
};

function _flash(container, type) {
  const wrap = container.querySelector('.theater-wrap');
  if (!wrap) return;

  const bg = document.createElement('div');
  bg.className = 'theater-flash-bg ' + type;
  wrap.appendChild(bg);

  const lbl = document.createElement('div');
  lbl.className = 'theater-flash-label ' + type;
  lbl.textContent = _FLASH_TEXT[type] || type.toUpperCase();
  wrap.appendChild(lbl);

  const dur = (type === 'deployed' || type === 'rolled-back') ? 2800 : 1800;
  setTimeout(() => { bg.remove(); lbl.remove(); }, dur);
}

/* ── Replay Engine ──────────────────────────────────────────── */
async function _playEvo(container, idx) {
  _stopReplay();
  _evoIdx = idx;
  const evo = _history[idx];
  if (!evo) return;

  container.querySelectorAll('.theater-tl-dot').forEach((d, i) => {
    d.classList.toggle('active', i === idx);
    if (i === idx) d.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
  });

  const term = container.querySelector('#theater-term');
  const surg = container.querySelector('#theater-surg');
  const fileEl = container.querySelector('#theater-file');
  const evoLabel = container.querySelector('#theater-evo-label');
  if (term) term.innerHTML = '';
  if (surg) surg.innerHTML = '';
  if (fileEl) fileEl.textContent = 'awaiting target...';
  if (evoLabel) {
    const desc = (evo.description || evo.id || '').slice(0, 60);
    evoLabel.textContent = desc;
  }

  _playing = true;
  _updatePlayBtn(container);
  _ecgMode = 'idle';

  let diffData = null;
  try { diffData = await api.get('/api/evolve/diff/' + evo.id); } catch {}
  const changes = diffData?.changes || evo.changes || [];

  const steps = [];
  const d = (ms) => Math.round(ms / _speed);

  steps.push({ fn: () => { _ecgMode = 'active'; _termLine(term, '⚡ EVOLUTION INITIATED', 'prompt'); }, wait: d(400) });
  steps.push({ fn: () => _termLine(term, evo.description || 'Self-modification cycle', 'info'), wait: d(900) });

  if (changes.length) {
    steps.push({ fn: () => { _ecgMode = 'elevated'; _termLine(term, `Targeting ${changes.length} file(s)...`); }, wait: d(700) });

    for (const ch of changes) {
      const file = ch.file || 'unknown';
      steps.push({
        fn: () => { _termLine(term, `→ ${file}`, 'highlight'); if (fileEl) fileEl.textContent = file; },
        wait: d(500),
      });
      if (ch.diff && ch.diff !== '(new file)') {
        const lineCount = ch.diff.split('\n').length;
        const diffTime = Math.min(lineCount * 60, 3000);
        steps.push({ fn: () => { _ecgMode = 'active'; _renderDiff(surg, ch.diff); }, wait: d(diffTime) });
      }
    }
  } else {
    steps.push({ fn: () => _termLine(term, '(no file-level diffs recorded)'), wait: d(500) });
  }

  if (evo.test_results) {
    steps.push({ fn: () => { _ecgMode = 'elevated'; _termLine(term, 'Running tests...'); }, wait: d(900) });
    if (evo.test_results.passed) {
      steps.push({ fn: () => { _ecgMode = 'active'; _termLine(term, '✓ All tests passed', 'success'); _flash(container, 'tests-passed'); }, wait: d(700) });
    } else {
      steps.push({ fn: () => { _ecgMode = 'flatline'; _termLine(term, '✗ Tests failed', 'error'); }, wait: d(700) });
    }
  }

  const pr = evo.pr_review || null;
  if (pr) {
    steps.push({ fn: () => { _ecgMode = 'elevated'; _termLine(term, 'Submitting PR for adversarial review...', 'prompt'); }, wait: d(1000) });
    steps.push({ fn: () => { _ecgMode = 'active'; _termLine(term, `PR ${pr.pr_id} — AI reviewer analyzing code`); }, wait: d(1200) });

    if (pr.inline_comments_count > 0 || pr.suggested_changes_count > 0) {
      const parts = [];
      if (pr.inline_comments_count) parts.push(`${pr.inline_comments_count} comment(s)`);
      if (pr.suggested_changes_count) parts.push(`${pr.suggested_changes_count} suggestion(s)`);
      steps.push({ fn: () => _termLine(term, `  Review findings: ${parts.join(', ')}`, 'highlight'), wait: d(800) });
    }

    if (pr.review_rounds > 1) {
      steps.push({ fn: () => _termLine(term, `  Review rounds: ${pr.review_rounds}`), wait: d(600) });
    }

    if (pr.reviewer_summary) {
      const summary = pr.reviewer_summary.split('\n')[0].slice(0, 120);
      steps.push({ fn: () => _termLine(term, `  Reviewer: "${summary}"`, 'info'), wait: d(1000) });
    }

    if (pr.verdict === 'approved') {
      steps.push({ fn: () => { _ecgMode = 'elevated'; _termLine(term, '✓ PR APPROVED — merging to main', 'success'); _flash(container, 'pr-approved'); }, wait: d(1000) });
    } else if (pr.verdict === 'rejected') {
      steps.push({ fn: () => { _ecgMode = 'flatline'; _termLine(term, '✗ PR REJECTED — queued for fix', 'error'); }, wait: d(1000) });
    } else if (pr.verdict === 'blocked') {
      steps.push({ fn: () => { _ecgMode = 'flatline'; _termLine(term, '⛔ PR BLOCKED — feature deferred', 'error'); }, wait: d(1000) });
    }
  }

  if (evo.status === 'deployed') {
    steps.push({ fn: () => { _ecgMode = 'spike'; _termLine(term, 'Deploying changes...'); }, wait: d(1400) });
    steps.push({ fn: () => {
      _termLine(term, '✓ DEPLOYED', 'success');
      _flash(container, 'deployed');
    }, wait: d(1800) });
    steps.push({ fn: () => { _ecgMode = 'flatline'; _termLine(term, 'Restarting Quinely process...'); }, wait: d(1200) });
    steps.push({ fn: () => {
      _ecgMode = 'idle';
      _termLine(term, '✓ SYSTEM RESTARTED — Quinely is live with new code', 'success');
      _flash(container, 'restarted');
    }, wait: d(1800) });
  } else if (evo.status === 'rolled_back') {
    steps.push({ fn: () => { _ecgMode = 'flatline'; _termLine(term, '↩ ROLLED BACK', 'error'); _flash(container, 'rolled-back'); }, wait: d(1500) });
    steps.push({ fn: () => { _ecgMode = 'idle'; }, wait: d(2000) });
  } else {
    steps.push({ fn: () => _termLine(term, `Status: ${evo.status}`, 'info'), wait: d(800) });
  }

  steps.push({ fn: () => { _playing = false; _updatePlayBtn(container); _termCursor(term); }, wait: d(400) });

  // execute sequentially with delays
  let cumulative = 300;
  for (const step of steps) {
    const t = cumulative;
    _timers.push(setTimeout(() => {
      if (!document.body.contains(container)) return;
      step.fn();
    }, t));
    cumulative += step.wait;
  }
}

function _stopReplay() {
  _timers.forEach(t => clearTimeout(t));
  _timers = [];
  _playing = false;
}

function _updatePlayBtn(container) {
  const btn = container.querySelector('#theater-play');
  if (!btn) return;
  if (_playing) {
    btn.innerHTML = `<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>`;
  } else {
    btn.innerHTML = `<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>`;
  }
}

/* ── HTML ───────────────────────────────────────────────────── */
function _html() {
  return `
<div class="theater-wrap">
  <div class="theater-heartbeat"><canvas id="theater-ecg"></canvas></div>

  <div class="theater-stats">
    <div class="theater-stat">
      <span class="theater-stat-val" id="ts-mods">0</span>
      <span class="theater-stat-lbl">self-modifications</span>
    </div>
    <div class="theater-stat">
      <span class="theater-stat-val" id="ts-healed">0</span>
      <span class="theater-stat-lbl">bugs self-healed</span>
    </div>
    <div class="theater-stat">
      <span class="theater-stat-val" id="ts-survived">0</span>
      <span class="theater-stat-lbl">crashes survived</span>
    </div>
    <div class="theater-stat">
      <span class="theater-stat-val theater-stat-zero" id="ts-human">0</span>
      <span class="theater-stat-lbl">human interventions</span>
    </div>
  </div>

  <div class="theater-main">
    <div class="theater-terminal">
      <div class="theater-term-bar">
        <span class="theater-bar-dot" style="background:#ef4444"></span>
        <span class="theater-bar-dot" style="background:#eab308"></span>
        <span class="theater-bar-dot" style="background:#22c55e"></span>
        <span class="theater-term-title">ghost — self-evolution</span>
      </div>
      <div class="theater-term-body" id="theater-term"></div>
    </div>
    <div class="theater-surgery">
      <div class="theater-surg-bar">
        <svg class="w-3.5 h-3.5 text-zinc-600 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"/></svg>
        <span id="theater-file" class="theater-file-label">awaiting target...</span>
      </div>
      <div class="theater-surg-body" id="theater-surg">
        <div class="theater-surg-empty">
          <div class="theater-surg-dna">
            <pre>
    ╱ ╲     ╱ ╲     ╱ ╲
   ╱   ╲   ╱   ╲   ╱   ╲
  ●─────● ●─────● ●─────●
   ╲   ╱   ╲   ╱   ╲   ╱
    ╲ ╱     ╲ ╱     ╲ ╱
   ●─────● ●─────● ●─────●
  ╱   ╲   ╱   ╲   ╱   ╲
 ╱     ╲ ╱     ╲ ╱     ╲</pre>
          </div>
          <div class="theater-surg-hint">Select an evolution to view code changes</div>
        </div>
      </div>
    </div>
  </div>

  <div class="theater-controls">
    <button id="theater-play" class="theater-ctrl-btn" title="Play / Pause">
      <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
    </button>
    <div class="theater-speed-group">
      <button class="theater-speed-btn active" data-speed="1">1×</button>
      <button class="theater-speed-btn" data-speed="2">2×</button>
      <button class="theater-speed-btn" data-speed="4">4×</button>
    </div>
    <span class="theater-evo-label" id="theater-evo-label">—</span>
  </div>

  <div class="theater-timeline" id="theater-tl"></div>
</div>`;
}

/* ── Entry Point ────────────────────────────────────────────── */
export async function render(container) {
  _cleanup();

  const [statsRes, histRes] = await Promise.all([
    api.get('/api/evolve/stats').catch(() => ({})),
    api.get('/api/evolve/history').catch(() => ({ history: [] })),
  ]);

  _stats = statsRes;
  _history = histRes.history || [];

  container.innerHTML = _html();

  _initCanvas(container);
  _animId = requestAnimationFrame(_drawEcg);

  _rollCounter(container.querySelector('#ts-mods'), _stats.total_evolutions || 0);
  _rollCounter(container.querySelector('#ts-healed'), _stats.deployed || 0);
  _rollCounter(container.querySelector('#ts-survived'), _stats.rolled_back || 0);

  // timeline
  const tl = container.querySelector('#theater-tl');
  if (tl) {
    _history.forEach((evo, i) => {
      const dot = document.createElement('button');
      dot.className = 'theater-tl-dot ' + (evo.status || 'planned');
      dot.title = (evo.description || evo.id || '').slice(0, 80) + ' (' + evo.status + ')';
      dot.addEventListener('click', () => _playEvo(container, i));
      tl.appendChild(dot);
    });
  }

  // controls
  const playBtn = container.querySelector('#theater-play');
  if (playBtn) {
    playBtn.addEventListener('click', () => {
      if (_playing) { _stopReplay(); _updatePlayBtn(container); }
      else if (_evoIdx >= 0) _playEvo(container, _evoIdx);
      else if (_history.length) _playEvo(container, _history.length - 1);
    });
  }

  container.querySelectorAll('.theater-speed-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      _speed = parseInt(btn.dataset.speed);
      container.querySelectorAll('.theater-speed-btn').forEach(b =>
        b.classList.toggle('active', b === btn)
      );
    });
  });

  // auto-play most recent
  if (_history.length) {
    setTimeout(() => _playEvo(container, _history.length - 1), 900);
  } else {
    const term = container.querySelector('#theater-term');
    _termLine(term, 'Awaiting first self-evolution...', 'prompt');
    _termCursor(term);
  }
}

function _cleanup() {
  if (_animId) { cancelAnimationFrame(_animId); _animId = null; }
  _stopReplay();
  _ecgHead = 0;
  _ecgLen = 0;
  _ecgT = 0;
  _ecgLastTs = 0;
  _ecgPrevMode = '';
  _ecgMode = 'idle';
  _evoIdx = -1;
}
