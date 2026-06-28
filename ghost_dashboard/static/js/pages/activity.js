/** Activity hub — one place to see what Ghost is doing, at three fidelities. */

import { mountHub } from './_hub.js';
import { render as feed } from './feed.js';
import { render as console_page } from './console.js';
import { render as traces } from './traces.js';

export async function render(container) {
  return mountHub(container, {
    storageKey: 'ghost-hub-activity',
    tabs: [
      { id: 'timeline', label: 'Timeline', render: feed, refreshMs: 5000 },
      { id: 'live', label: 'Live', render: console_page },
      { id: 'traces', label: 'Traces', render: traces },
    ],
  });
}
