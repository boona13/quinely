/** Evolution hub — the full self-improvement pipeline under one roof.
 *
 * Backlog (Future Features) → Approvals/History (Evolution) → Pull Requests,
 * plus the live theater view and the autonomous action-item queue. Each tab
 * delegates to its existing page module, so no behavior changes — only the
 * five separate nav entries collapse into one. */

import { mountHub } from './_hub.js';
import { render as evolve_theater } from './evolve_theater.js';
import { render as future_features } from './future_features.js';
import { render as prs } from './prs.js';
import { render as evolve } from './evolve.js';
import { render as autonomy } from './autonomy.js';

export async function render(container) {
  return mountHub(container, {
    storageKey: 'ghost-hub-evolution',
    defaultTab: 'history',
    tabs: [
      { id: 'live', label: 'Live', render: evolve_theater },
      { id: 'backlog', label: 'Backlog', render: future_features },
      { id: 'history', label: 'History', render: evolve },
      { id: 'prs', label: 'Pull Requests', render: prs },
      { id: 'actions', label: 'Action Items', render: autonomy },
    ],
  });
}
