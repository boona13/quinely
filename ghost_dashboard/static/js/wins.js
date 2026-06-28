/**
 * Wins — celebrate Ghost's autonomous milestones.
 *
 * Taps the existing console SSE stream and fires a tasteful celebration toast
 * the moment Ghost does something noteworthy on its own (evolves itself, learns
 * a skill, completes a goal, heals a bug, ships a PR). Purely additive: if no
 * milestone events arrive, nothing happens.
 */

const SPARKLE = `<svg viewBox="0 0 24 24" fill="none" width="16" height="16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/></svg>`;

// High-signal milestone rules. Matched against the event title/detail/result.
// Keep these specific so routine successes don't trigger confetti fatigue.
const RULES = [
  { re: /\b(deployed|shipped a build|hot-?reload|evolution (complete|deployed)|self-?evolved)\b/i, title: 'Ghost evolved itself', kind: 'evolve' },
  { re: /\bself-?heal(ed)?\b|\b(bug|error|crash|regression)\b[^.]{0,40}\b(fixed|resolved|repaired|healed)\b/i, title: 'Ghost healed itself', kind: 'heal' },
  { re: /\b(new skill|skill (added|learned|created|installed)|learned a (new )?skill)\b/i, title: 'Ghost learned a new skill', kind: 'skill' },
  { re: /\b(goal)\b[^.]{0,40}\b(complete|completed|achieved|finished|done)\b/i, title: 'Ghost completed a goal', kind: 'goal' },
  { re: /\b(pull request|\bPR\b)\b[^.]{0,40}\b(merged|approved|opened|submitted)\b/i, title: 'Ghost shipped a pull request', kind: 'pr' },
];

let lastSeq = -1;
let primed = false;
const queue = [];
let draining = false;

function fieldText(evt) {
  return `${evt.title || ''} ${evt.detail || ''} ${evt.result || ''}`.trim();
}

function classify(evt) {
  // Only consider positive or growth-flavoured events.
  const level = (evt.level || '').toLowerCase();
  const cat = (evt.category || '').toLowerCase();
  if (level === 'error' || level === 'warn') return null;
  if (level !== 'success' && cat !== 'growth') return null;

  const text = fieldText(evt);
  for (const rule of RULES) {
    if (rule.re.test(text)) {
      const sub = (evt.detail || evt.result || evt.title || '').slice(0, 90);
      return { title: rule.title, sub };
    }
  }
  return null;
}

function celebrate({ title, sub }) {
  const host = document.getElementById('toast-container');
  if (!host) return;
  const el = document.createElement('div');
  el.className = 'toast toast-celebrate';
  el.innerHTML = `
    <span class="celebrate-icon">${SPARKLE}</span>
    <span class="celebrate-body">
      <div class="celebrate-title"></div>
      ${sub ? '<div class="celebrate-sub"></div>' : ''}
    </span>`;
  el.querySelector('.celebrate-title').textContent = title;
  if (sub) el.querySelector('.celebrate-sub').textContent = sub;
  host.appendChild(el);
  setTimeout(() => el.remove(), 5200);
}

function drain() {
  if (draining) return;
  const next = queue.shift();
  if (!next) return;
  draining = true;
  celebrate(next);
  setTimeout(() => { draining = false; drain(); }, 3600);
}

function enqueue(win) {
  if (queue.length >= 3) return; // avoid celebration spam during bursts
  queue.push(win);
  drain();
}

function handleEvent(evt) {
  if (typeof evt.seq === 'number') {
    if (evt.seq <= lastSeq) return; // already seen (or replay burst)
    lastSeq = evt.seq;
  }
  if (!primed) return; // skip everything that existed before this session
  const win = classify(evt);
  if (win) enqueue(win);
}

async function start() {
  // Establish a baseline so the SSE replay burst doesn't trigger toasts.
  try {
    const hist = await window.GhostAPI.get('/api/console/history?limit=1');
    const evts = hist.events || [];
    if (evts.length) lastSeq = evts[evts.length - 1].seq ?? -1;
  } catch {}

  const source = new EventSource('/api/console/stream');
  source.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch {}
  };
  source.onerror = () => {
    // EventSource auto-reconnects; lastSeq guards against re-celebrating.
  };
  // Let the initial burst flush past the seq guard before we start celebrating.
  setTimeout(() => { primed = true; }, 1500);
}

export function initWins() {
  if (window.__ghostWinsStarted) return;
  window.__ghostWinsStarted = true;
  start();
}
