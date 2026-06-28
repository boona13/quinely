/** Memory hub — raw FTS entries and the synthesized structured-memory profile. */

import { mountHub } from './_hub.js';
import { render as memory } from './memory.js';
import { render as structured_memory } from './structured_memory.js';
import { render as memory_map } from './memory_map.js';

export async function render(container) {
  return mountHub(container, {
    storageKey: 'ghost-hub-memory',
    tabs: [
      { id: 'map', label: 'Map', render: memory_map },
      { id: 'entries', label: 'Entries', render: memory },
      { id: 'structured', label: 'Structured', render: structured_memory },
    ],
  });
}
