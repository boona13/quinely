/** Quinely Dashboard API client */

// Get CSRF token from meta tag
function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
}

export const api = {
  async get(url) {
    const r = await fetch(url);
    return r.json();
  },

  async put(url, data) {
    const r = await fetch(url, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrfToken(),
      },
      body: JSON.stringify(data),
    });
    return r.json();
  },

  async post(url, data = {}) {
    const r = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrfToken(),
      },
      body: JSON.stringify(data),
    });
    return r.json();
  },

  async postRaw(url, data = {}, extraHeaders = {}) {
    const r = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrfToken(),
        ...extraHeaders,
      },
      body: JSON.stringify(data),
    });
    return r.json();
  },

  async patch(url, data = {}) {
    const r = await fetch(url, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrfToken(),
      },
      body: JSON.stringify(data),
    });
    return r.json();
  },

  async del(url) {
    const r = await fetch(url, {
      method: 'DELETE',
      headers: {
        'X-CSRFToken': getCsrfToken(),
      },
    });
    return r.json();
  },
};

window.GhostAPI = api;
