/**
 * Ticker 表示メインスクリプト (Ticker/js/info.js)
 * - OBS WebSocket 経由の CustomEvent (UPDATE_TICKER) 受信 (shared/obs-client.js 使用)
 * - シーン切替連動 (CurrentProgramSceneChanged) による定型文自動反映
 * - BroadcastChannel / localStorage / HTTP API (/api/config) による多重同期
 */
(() => {
  'use strict';

  const env = window.OBSWidgetsEnv || {};
  const DEFAULT_ADDRS = env.OBS_FALLBACK_ADDRESSES || ['ws://127.0.0.1:4455', 'ws://localhost:4455'];

  const CONFIG = {
    speedPxPerSec: 80,
    storageKey: 'stream_ticker_message_v1',
    speedKey: 'stream_ticker_speed_v1',
    presetsKey: 'stream_ticker_presets_v2',
    channelName: 'stream_ticker_broadcast_v1',
    wsAddresses: DEFAULT_ADDRS,
    wsPassword: env.OBS_DEFAULT_PASSWORD || '',

    // シーン名定義
    sceneStarting: (env.OBS_SCENE_NAMES && env.OBS_SCENE_NAMES.starting) || 'Starting',
    sceneLiveMain: (env.OBS_SCENE_NAMES && env.OBS_SCENE_NAMES.liveMain) || 'Live Main',
    sceneLiveChat: (env.OBS_SCENE_NAMES && env.OBS_SCENE_NAMES.liveChat) || 'Live Chat',
    sceneEnding:   (env.OBS_SCENE_NAMES && env.OBS_SCENE_NAMES.ending)   || 'Ending',

    // シーン別デフォルト定型文
    defaultPresets: [
      { label: '開始挨拶', msg: 'ただいま配信開始準備中です。しばらくおまちください。' },
      { label: '通常挨拶', msg: '配信へのご参加ありがとうございます！コメントやフォローはお気軽にどうぞ✨' },
      { label: '離席中',   msg: 'ただいま少し離席中です。すぐに戻りますので少々お待ちください☕' },
      { label: '配信終了', msg: '本日の配信は終了しました。ご視聴ありがとうございました！次回もお楽しみに👋' }
    ],
    defaultText: '配信へのご参加ありがとうございます！コメントやフォローはお気軽にどうぞ✨'
  };

  const track = document.getElementById('ticker-track');
  const originalItem = document.getElementById('ticker-item');
  let currentClone = null;

  // 状態復元
  let currentText = localStorage.getItem(CONFIG.storageKey) || CONFIG.defaultText;
  let currentSpeed = parseInt(localStorage.getItem(CONFIG.speedKey), 10) || CONFIG.speedPxPerSec;

  // 保存されている定型文プリセットを取得（編集済みの場合はそれを反映）
  function getPresets() {
    try {
      const raw = localStorage.getItem(CONFIG.presetsKey);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed) && parsed.length >= 4) {
          return parsed;
        }
      }
    } catch (e) {}
    return CONFIG.defaultPresets;
  }

  // シーン名に対応するティッカー定型文を返す
  function getTextForScene(sceneName) {
    const presets = getPresets();
    if (sceneName === CONFIG.sceneStarting) {
      return (presets[0] && presets[0].msg) || CONFIG.defaultPresets[0].msg;
    }
    if (sceneName === CONFIG.sceneLiveMain || sceneName === CONFIG.sceneLiveChat) {
      return (presets[1] && presets[1].msg) || CONFIG.defaultPresets[1].msg;
    }
    if (sceneName === CONFIG.sceneEnding) {
      return (presets[3] && presets[3].msg) || CONFIG.defaultPresets[3].msg;
    }
    return null;
  }

  // 表示更新
  function updateTicker(text) {
    if (!text || text === currentText && originalItem.textContent.trim() !== '') return;
    currentText = text;
    try {
      localStorage.setItem(CONFIG.storageKey, text);
    } catch (e) {}

    originalItem.innerHTML = '';
    originalItem.appendChild(document.createTextNode(text + ' '));
    const sep = document.createElement('span');
    sep.className = 'ticker-separator';
    sep.textContent = '◆';
    originalItem.appendChild(sep);

    if (currentClone) currentClone.remove();
    currentClone = originalItem.cloneNode(true);
    currentClone.removeAttribute('id');
    currentClone.setAttribute('aria-hidden', 'true');
    track.appendChild(currentClone);

    applyConstantSpeed();
  }

  function applyConstantSpeed() {
    track.style.animation = 'none';
    track.offsetHeight; // reflow

    const distancePx = originalItem.offsetWidth;
    const speed = Math.max(10, currentSpeed);
    const durationSec = distancePx / speed;

    track.style.animation = '';
    track.style.animationDuration = `${durationSec}s`;
  }

  // シーン由来の文面はサーバーの current_ticker にも保存する (Issue #180)。
  // 保存しないと 3 秒ポーリング (syncServerTicker) がサーバー値 (旧文面) で
  // 表示を巻き戻してしまう。シーン→文面の対応付けは Ticker 側に一元化し、
  // quick-dock 側には持たせない (quick-dock は離席トグルの保存のみ担当)。
  function saveTickerToServer(text) {
    try {
      const doFetch = (typeof window.authFetch === 'function') ? window.authFetch : fetch;
      doFetch('/api/twitch/save-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_ticker: text, ticker_speed: currentSpeed })
      }).catch(() => {});
    } catch (e) {}
  }

  function applySceneTicker(sceneName) {
    const sceneMsg = getTextForScene(sceneName);
    if (!sceneMsg) return;
    updateTicker(sceneMsg);
    saveTickerToServer(sceneMsg);
  }

  // 初回表示
  updateTicker(currentText);

  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(applyConstantSpeed);
  } else {
    window.addEventListener('load', applyConstantSpeed);
  }

  /* ========================================================
   * 1. OBS WebSocket 連携 (shared/obs-client.js 共通ラッパー使用)
   * - iPad等LAN端末からの BroadcastCustomEvent 受信
   * - シーン切替連動
   * ======================================================== */
  if (window.OBSWidgetsClient && typeof window.OBSWidgetsClient.createManagedObsClient === 'function') {
    const client = window.OBSWidgetsClient.createManagedObsClient({
      addresses: CONFIG.wsAddresses,
      password: CONFIG.wsPassword,
      reconnectDelayMs: 3000,
      setupListeners: (obs) => {
        obs.on('CurrentProgramSceneChanged', (data) => {
          if (data && data.sceneName) {
            applySceneTicker(data.sceneName);
          }
        });

        obs.on('CustomEvent', (raw) => {
          if (!raw) return;
          const data = (raw && raw.eventData) || raw;
          if (data && (data.type === 'UPDATE_TICKER' || data.realm === 'obs-stream-ticker-v1')) {
            if (typeof data.text === 'string' && data.text.trim() !== '') {
              updateTicker(data.text.trim());
            }
            if (typeof data.speed === 'number' && data.speed > 0) {
              currentSpeed = data.speed;
              applyConstantSpeed();
            }
          }
        });
      },
      onConnected: async (obs) => {
        // 接続時は表示だけ合わせ、サーバーへは保存しない (Issue #180)。
        // リロード時にサーバー側の離席文面・カスタム文面をシーン定型文で
        // 潰さないためで、保存はシーン切替イベント (applySceneTicker) 側で行う。
        try {
          const cur = await obs.call('GetCurrentProgramScene');
          if (cur && cur.currentProgramSceneName) {
            const sceneMsg = getTextForScene(cur.currentProgramSceneName);
            if (sceneMsg) {
              updateTicker(sceneMsg);
            }
          }
        } catch (e) {}
      }
    });

    client.connect();
  }

  /* ========================================================
   * 2. BroadcastChannel（同一PC・同一オリジン即時同期）
   * ======================================================== */
  try {
    const broadcast = new BroadcastChannel(CONFIG.channelName);
    broadcast.onmessage = (event) => {
      if (!event.data) return;
      const d = event.data;
      if ((d.type === 'UPDATE_TICKER' || d.type === 'UPDATE_TEXT') && typeof d.text === 'string') {
        updateTicker(d.text);
      }
      if ((d.type === 'UPDATE_TICKER' || d.type === 'UPDATE_SPEED') && typeof d.speed === 'number') {
        currentSpeed = d.speed;
        applyConstantSpeed();
      }
    };
  } catch (e) {}

  /* ========================================================
   * 3. storage イベント受信（同一ブラウザタブ間同期）
   * ======================================================== */
  window.addEventListener('storage', (e) => {
    if (e.key === CONFIG.storageKey && e.newValue) {
      updateTicker(e.newValue);
    } else if (e.key === CONFIG.speedKey && e.newValue) {
      currentSpeed = parseInt(e.newValue, 10) || 80;
      applyConstantSpeed();
    }
  });

  /* ========================================================
   * 4. サーバーAPI (/api/config) 同期
   * - iPad等の別端末からHTTP保存された最新ティッカー＆定型文プリセットを拾う
   * ======================================================== */
  async function syncServerTicker() {
    try {
      const res = await fetch('/api/config', { cache: 'no-store' });
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data.ticker_presets) && data.ticker_presets.length >= 4) {
          try {
            localStorage.setItem(CONFIG.presetsKey, JSON.stringify(data.ticker_presets));
          } catch (_) {}
        }
        if (data.current_ticker && data.current_ticker.trim() !== '' && data.current_ticker !== currentText) {
          updateTicker(data.current_ticker.trim());
        }
        if (typeof data.ticker_speed === 'number' && data.ticker_speed > 0 && data.ticker_speed !== currentSpeed) {
          currentSpeed = data.ticker_speed;
          applyConstantSpeed();
        }
      }
    } catch (e) {}
  }

  setInterval(syncServerTicker, 3000);
  setTimeout(syncServerTicker, 1500);

})();
