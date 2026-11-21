/**
 * OBS WebSocket 安全接続ラッパー (/shared/obs-client.js)
 * - パスワード未設定時の /api/config 自動解決フォールバック
 * - 再接続タイマーの多重起動防止（排他制御）
 * - 再接続ごとのイベントリスナー重複登録（メモリリーク）防止
 * - 複数候補アドレス（LAN IP / 127.0.0.1 / localhost）の自動フォールバック接続
 */
(() => {
  'use strict';

  const env = window.OBSWidgetsEnv || {};
  const DEFAULT_ADDRS = env.OBS_FALLBACK_ADDRESSES || ['ws://127.0.0.1:4455', 'ws://localhost:4455'];
  const DEFAULT_PASS = env.OBS_DEFAULT_PASSWORD || '';
  const API_BASE = (typeof location !== 'undefined' && (location.protocol === 'http:' || location.protocol === 'https:'))
    ? (env.API_BASE || '')
    : 'http://localhost:8080';

  function createManagedObsClient({
    addresses = DEFAULT_ADDRS,
    password = DEFAULT_PASS,
    eventSubscriptions = undefined,
    reconnectDelayMs = 3000,
    setupListeners = null,
    onConnected = null,
    onDisconnected = null
  } = {}) {
    const addrList = Array.isArray(addresses) && addresses.length > 0
      ? addresses
      : DEFAULT_ADDRS;

    let obs = null;
    let isConnected = false;
    let isConnecting = false;
    let reconnectTimer = null;
    let addrIndex = 0;
    let resolvedPassword = password || '';

    function clearReconnectTimer() {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    }

    function scheduleReconnect(delay = reconnectDelayMs) {
      clearReconnectTimer();
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    }

    async function resolvePasswordIfNeeded() {
      if (resolvedPassword) return resolvedPassword;
      const fromLocal = (window.OBS_LOCAL_ENV && window.OBS_LOCAL_ENV.OBS_DEFAULT_PASSWORD)
        || (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_DEFAULT_PASSWORD)
        || '';
      if (fromLocal) {
        resolvedPassword = fromLocal;
        return resolvedPassword;
      }
      try {
        const res = await (window.authFetch || fetch)(`${API_BASE}/api/config`, { cache: 'no-store' });
        if (res.ok) {
          const data = await res.json();
          if (data && data.obs_password) {
            resolvedPassword = data.obs_password;
          }
        }
      } catch (_) {}
      return resolvedPassword;
    }

    async function connect() {
      if (isConnecting || isConnected) return;
      clearReconnectTimer();

      if (typeof OBSWebSocket === 'undefined') {
        if (onDisconnected) onDisconnected(new Error('OBSWebSocket library not loaded'));
        scheduleReconnect(reconnectDelayMs);
        return;
      }

      isConnecting = true;
      await resolvePasswordIfNeeded();

      if (obs) {
        try { obs.removeAllListeners(); } catch (_) {}
        try { await obs.disconnect(); } catch (_) {}
      }

      obs = new OBSWebSocket();

      if (typeof setupListeners === 'function') {
        setupListeners(obs);
      }

      obs.on('ConnectionClosed', () => {
        const wasConnected = isConnected;
        isConnected = false;
        isConnecting = false;
        if (wasConnected && typeof onDisconnected === 'function') {
          try { onDisconnected(); } catch (_) {}
        }
        scheduleReconnect(reconnectDelayMs);
      });

      const targetAddr = addrList[addrIndex % addrList.length];
      try {
        const connectOpts = eventSubscriptions !== undefined ? { eventSubscriptions } : undefined;
        await obs.connect(targetAddr, resolvedPassword, connectOpts);
      } catch (err) {
        isConnected = false;
        isConnecting = false;
        addrIndex = (addrIndex + 1) % addrList.length;
        if (typeof onDisconnected === 'function') {
          try { onDisconnected(err); } catch (_) {}
        }
        scheduleReconnect(reconnectDelayMs);
        return;
      }
      isConnected = true;
      isConnecting = false;
      // 接続後のページ側初期化は、接続の成否判定と切り離す (Issue #81)。
      // 初期化の例外で正常な接続まで切断すると、再接続ループになる。
      if (typeof onConnected === 'function') {
        try {
          await onConnected(obs, targetAddr);
        } catch (err) {
          console.error('[obs-client] onConnected initialization failed:', err);
        }
      }
    }

    async function call(requestType, requestData = {}) {
      if (!isConnected || !obs) return null;
      return await obs.call(requestType, requestData);
    }

    return {
      connect,
      call,
      scheduleReconnect,
      get instance() { return obs; },
      get connected() { return isConnected; }
    };
  }

  window.OBSWidgetsClient = { createManagedObsClient };
})();
