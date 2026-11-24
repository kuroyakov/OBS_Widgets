/**
 * shared/api-token.js
 * 状態変更 API 用の共有トークン解決と fetch ラッパー (Issue #46)
 *
 * 解決順:
 *   1. localStorage の保存値（iPad で初回に手入力したトークン等）
 *   2. localhost で開いている場合のみ /api/auth/token から自動取得して保存
 * LAN 上のページでは自動取得できないため、操作ページの設定欄から入力する。
 */
(function () {
  const STORAGE_KEY = 'widgetApiToken';
  let cached = null;
  let inflight = null;

  function isLocalHostName() {
    return ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
  }

  function readStored() {
    try { return localStorage.getItem(STORAGE_KEY) || ''; } catch (e) { return ''; }
  }

  async function resolveToken() {
    const stored = readStored();
    if (stored) { cached = stored; return cached; }
    if (isLocalHostName()) {
      try {
        const res = await fetch('/api/auth/token', { cache: 'no-store' });
        if (res.ok) {
          const data = await res.json();
          if (data && data.api_token) {
            cached = data.api_token;
            try { localStorage.setItem(STORAGE_KEY, cached); } catch (e) {}
            return cached;
          }
        }
      } catch (e) {}
    }
    cached = '';
    return cached;
  }

  window.getWidgetApiToken = function () {
    if (cached !== null) return Promise.resolve(cached);
    if (!inflight) inflight = resolveToken();
    return inflight;
  };

  window.setWidgetApiToken = function (token) {
    cached = token || '';
    inflight = null;
    try {
      if (token) localStorage.setItem(STORAGE_KEY, token);
      else localStorage.removeItem(STORAGE_KEY);
    } catch (e) {}
  };

  function isAllowedTokenOrigin(url) {
    let parsed;
    try {
      parsed = new URL(url, location.href);
    } catch (e) {
      return false;
    }
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      return parsed.origin === location.origin;
    }
    if (location.protocol === 'file:') {
      if (parsed.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname)) {
        return true;
      }
      const envApiBase = window.OBSWidgetsEnv && typeof window.OBSWidgetsEnv.API_BASE === 'string'
        ? window.OBSWidgetsEnv.API_BASE
        : '';
      if (envApiBase) {
        try {
          const baseOrigin = new URL(envApiBase, location.href).origin;
          if (baseOrigin && baseOrigin !== 'null' && parsed.origin === baseOrigin) {
            return true;
          }
        } catch (e) {}
      }
      return false;
    }
    return false;
  }

  window.authFetch = async function (url, options) {
    const opts = Object.assign({}, options || {});
    if (isAllowedTokenOrigin(url)) {
      const token = await window.getWidgetApiToken();
      if (token) {
        opts.headers = Object.assign({}, opts.headers || {}, { 'X-Widget-Token': token });
      }
    }
    return fetch(url, opts);
  };
})();
