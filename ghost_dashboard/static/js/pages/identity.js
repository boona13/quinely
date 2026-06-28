/** Identity hub — Ghost's personality (SOUL.md) and what it knows about you (USER.md). */

import { mountHub } from './_hub.js';
import { render as soul } from './soul.js';
import { render as user } from './user.js';

export async function render(container) {
  return mountHub(container, {
    storageKey: 'ghost-hub-identity',
    tabs: [
      { id: 'personality', label: 'Personality', render: soul },
      { id: 'about', label: 'About You', render: user },
    ],
  });
}
