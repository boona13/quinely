/**
 * Quinely Dashboard i18n — Lightweight client-side localization.
 *
 * Usage:
 *   import { i18n } from './i18n/index.js';
 *   i18n.t('nav.chat')           // "Chat"
 *   i18n.t('common.save')        // "Save"
 *   i18n.t('common.items', {n: 5}) // "5 items"
 *   i18n.setLocale('ar')         // switches to Arabic + RTL
 */

import en from './locales/en.js';

const SUPPORTED_LOCALES = {
  en:      { label: 'English',        dir: 'ltr' },
  ar:      { label: 'العربية',        dir: 'rtl' },
  'zh-CN': { label: '简体中文',       dir: 'ltr' },
  'pt-BR': { label: 'Português (BR)', dir: 'ltr' },
};

const STORAGE_KEY = 'ghost.i18n.locale';
const _listeners = new Set();

let _currentLocale = 'en';
let _strings = en;
let _fallback = en;

function _resolveInitialLocale() {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored && SUPPORTED_LOCALES[stored]) return stored;

  const nav = navigator.language || navigator.userLanguage || 'en';
  if (SUPPORTED_LOCALES[nav]) return nav;

  const prefix = nav.split('-')[0];
  if (prefix === 'ar') return 'ar';
  if (prefix === 'zh') return 'zh-CN';
  if (prefix === 'pt') return 'pt-BR';

  return 'en';
}

function _deepGet(obj, path) {
  const parts = path.split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[p];
  }
  return cur;
}

function _interpolate(str, params) {
  if (!params || typeof str !== 'string') return str;
  return str.replace(/\{(\w+)\}/g, (_, key) =>
    params[key] !== undefined ? String(params[key]) : `{${key}}`
  );
}

export const i18n = {
  t(key, params) {
    let val = _deepGet(_strings, key);
    if (val === undefined) val = _deepGet(_fallback, key);
    if (val === undefined) return key;
    return _interpolate(val, params);
  },

  getLocale() {
    return _currentLocale;
  },

  getDir() {
    return (SUPPORTED_LOCALES[_currentLocale] || {}).dir || 'ltr';
  },

  getSupportedLocales() {
    return Object.entries(SUPPORTED_LOCALES).map(([code, meta]) => ({
      code,
      label: meta.label,
      dir: meta.dir,
    }));
  },

  async setLocale(locale) {
    if (!SUPPORTED_LOCALES[locale]) return;
    if (locale === _currentLocale && _strings !== _fallback) return;

    if (locale === 'en') {
      _strings = en;
    } else {
      try {
        const mod = await import(`./locales/${locale}.js`);
        _strings = mod.default || mod;
      } catch (e) {
        console.warn(`[i18n] Failed to load locale ${locale}:`, e);
        _strings = en;
        locale = 'en';
      }
    }

    _currentLocale = locale;
    localStorage.setItem(STORAGE_KEY, locale);

    const dir = i18n.getDir();
    document.documentElement.dir = dir;
    document.documentElement.lang = locale;
    document.body.classList.toggle('rtl', dir === 'rtl');

    i18n.applyTranslations();
    _listeners.forEach(fn => { try { fn(locale); } catch (e) { console.error(e); } });
  },

  applyTranslations() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      const translated = i18n.t(key);
      if (translated !== key) {
        el.textContent = translated;
      }
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
      const key = el.getAttribute('data-i18n-title');
      const translated = i18n.t(key);
      if (translated !== key) {
        el.title = translated;
      }
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      const translated = i18n.t(key);
      if (translated !== key) {
        el.placeholder = translated;
      }
    });
  },

  onChange(fn) {
    _listeners.add(fn);
    return () => _listeners.delete(fn);
  },

  async init() {
    const locale = _resolveInitialLocale();
    await i18n.setLocale(locale);
  },
};
