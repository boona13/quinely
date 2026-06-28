/** Security hub — audit posture and the sensitive-operation audit log. */

import { mountHub } from './_hub.js';
import { render as security } from './security.js';
import { render as audit } from './audit.js';

export async function render(container) {
  return mountHub(container, {
    storageKey: 'ghost-hub-security',
    tabs: [
      { id: 'posture', label: 'Posture', render: security },
      { id: 'audit', label: 'Audit Log', render: audit },
    ],
  });
}
