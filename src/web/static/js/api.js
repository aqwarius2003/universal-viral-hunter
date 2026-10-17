/**
 * Centralized REST API Client
 */
import { showToast } from './toast.js';

export const api = {
  async get(url, options = {}) {
    return this.request(url, { method: 'GET', ...options });
  },

  async post(url, body = {}, options = {}) {
    return this.request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      ...options
    });
  },

  async put(url, body = {}, options = {}) {
    return this.request(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      ...options
    });
  },

  async delete(url, options = {}) {
    return this.request(url, { method: 'DELETE', ...options });
  },

  async request(url, options = {}) {
    try {
      const res = await fetch(url, {
        credentials: 'same-origin',
        ...options
      });
      
      const contentType = res.headers.get('content-type') || '';
      let data = null;
      if (contentType.includes('application/json')) {
        data = await res.json();
      } else {
        data = await res.text();
      }

      if (!res.ok) {
        const errorMsg = (data && data.error) ? data.error : (data && data.detail) ? data.detail : `HTTP ${res.status}: ${res.statusText}`;
        throw new Error(errorMsg);
      }

      return data;
    } catch (err) {
      console.error(`API Error [${options.method || 'GET'} ${url}]:`, err);
      throw err;
    }
  }
};

window.api = api;
