/** Memory hub — raw FTS entries and the synthesized structured-memory profile. */

import { mountHub } from './_hub.js';
import { render as memory } from './memory.js';
import { render as structured_memory } from './structured_memory.js';

export async function render(container) {
  return mountHub(container, {
    storageKey: 'ghost-hub-memory',
    tabs: [
      { id: 'entries', label: 'Entries', render: memory },
      { id: 'structured', label: 'Structured', render: structured_memory },
    ],
  });
}
