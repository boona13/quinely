/** AI Nodes hub — the local capability catalog and the media it generates. */

import { mountHub } from './_hub.js';
import { render as nodes } from './nodes.js';
import { render as gallery } from './gallery.js';

export async function render(container) {
  return mountHub(container, {
    storageKey: 'ghost-hub-nodes',
    tabs: [
      { id: 'catalog', label: 'Catalog', render: nodes },
      { id: 'outputs', label: 'Outputs', render: gallery },
    ],
  });
}
