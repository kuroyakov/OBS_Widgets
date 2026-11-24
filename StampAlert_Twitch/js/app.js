/* ========================================================
 * 設定
 * ======================================================== */
const CONFIG = {
  // 対象チャンネルは URL パラメータ (?channel=xxx) を最優先する (README 記載、Issue #183)。
  // 未指定・空の場合はローカル環境のチャンネル、それも無ければデフォルトを使う。
  // 空白のみのパラメータも未指定としてフォールバックさせるため trim してから判定する（PR #190 round-1 レビュー指摘）。
  channel: (new URLSearchParams(location.search).get('channel') || '').trim()
    || (window.OBS_LOCAL_ENV && window.OBS_LOCAL_ENV.TWITCH_CHANNEL)
    || 'your_channel',

  // 定位置の高さ（Y軸）の出現範囲（%）
  minTopPercent: 20,
  maxTopPercent: 70,

  // 複数スタンプ連射間隔（ミリ秒）
  staggerDelayMs: 120,

  // ★ 斜め軌道の高低差（px）：400px〜700pxの大きな高低差で明確な斜め軌道にします ★
  minAngleOffsetPx: 400,
  maxAngleOffsetPx: 700
};

(() => {
  'use strict';

  const stage = document.getElementById('stage');
  const connStatus = document.getElementById('conn-status');
  const logDisplay = document.getElementById('log-display');
  const monitorPanel = document.getElementById('monitor-panel');

  window.addEventListener('keydown', (e) => {
    if (e.key === 'h' || e.key === 'H') {
      monitorPanel.classList.toggle('hidden');
    }
  });

  function setLog(msg) {
    logDisplay.textContent = msg;
  }

  /* ========================================================
   * 画像プリロード完了後に斜めカットイン
   * ======================================================== */
  function spawnStamp(imageUrl) {
    if (!imageUrl || typeof imageUrl !== 'string') return;

    try {
      const img = new Image();
      img.className = 'stamp-img';
      img.src = imageUrl;

      const startAnimation = () => {
        const item = document.createElement('div');
        item.className = 'stamp-item';

        // 1. 定位置（Y軸）のランダム決定
        const randomTop = Math.floor(
          Math.random() * (CONFIG.maxTopPercent - CONFIG.minTopPercent + 1) + CONFIG.minTopPercent
        );
        item.style.top = `${randomTop}%`;

        // 2. ★ 明確な斜めにするための高低差（400px〜700px）を計算 ★
        const isFromTop = Math.random() < 0.5;
        const offsetMagnitude = Math.floor(
          Math.random() * (CONFIG.maxAngleOffsetPx - CONFIG.minAngleOffsetPx + 1) + CONFIG.minAngleOffsetPx
        );
        const startY = isFromTop ? -offsetMagnitude : offsetMagnitude;

        // 3. ★ X移動量（約2100px）とY高低差から、実際の飛行角度を三角関数で自動算出 ★
        const screenWidth = window.innerWidth || 1920;
        const distanceX = screenWidth + 200;
        // 角度（度数法）: 約 -11度〜-19度 または +11度〜+19度
        const angleDeg = Math.round(Math.atan2(startY, distanceX) * (180 / Math.PI));

        item.style.setProperty('--start-y', `${startY}px`);
        item.style.setProperty('--fly-angle', `${angleDeg}deg`);

        // 同時表示上限（40個）を超えたら古いスタンプを即座に破棄（非表示シーンでのDOM蓄積防止）
        while (stage.children.length >= 40) {
          stage.firstElementChild.remove();
        }

        item.appendChild(img);
        stage.appendChild(item);

        // CSS animationend と 強制削除タイマー(6.5秒) の二重ガードで確実にDOM解放
        let removed = false;
        const cleanup = () => {
          if (removed) return;
          removed = true;
          if (item.parentNode) item.remove();
        };
        const fallbackTimer = setTimeout(cleanup, 6500);
        item.addEventListener('animationend', () => {
          clearTimeout(fallbackTimer);
          cleanup();
        }, { once: true });
      };

      if ('decode' in img) {
        img.decode()
          .then(startAnimation)
          .catch(() => {
            img.onload = startAnimation;
          });
      } else {
        img.onload = startAnimation;
      }
    } catch (err) {
      console.error('spawnStamp error:', err);
    }
  }

  /* ========================================================
   * 複数個・連投スタンプの全抽出＆射出
   * ======================================================== */
  const MAX_EMOTES_PER_MSG = 20;
  const VALID_EMOTE_ID_RE = /^[A-Za-z0-9_-]+$/;
  const VALID_CHANNEL_RE = /^[a-z0-9_]{1,25}$/;

  function parseEmotesFromLine(line) {
    // IRC メッセージ先頭のタグ区画 (@... ) のみから emotes タグを抽出する
    if (!line || !line.startsWith('@')) return;
    const spaceIdx = line.indexOf(' ');
    const tagSection = spaceIdx >= 0 ? line.slice(1, spaceIdx) : line.slice(1);
    const match = /(?:^|;)emotes=([^;]+)/.exec(tagSection);
    if (!match) return;

    const emotesRaw = match.at(1);
    if (!emotesRaw || emotesRaw.trim() === '') return;

    const urlsToSpawn = [];
    const emoteGroups = emotesRaw.split('/');

    for (const group of emoteGroups) {
      if (urlsToSpawn.length >= MAX_EMOTES_PER_MSG) break;
      const parts = group.split(':');
      const emoteId = parts.at(0);
      const occurrences = parts.at(1);

      if (emoteId && VALID_EMOTE_ID_RE.test(emoteId) && occurrences) {
        const count = occurrences.split(',').length;
        const emoteUrl = `https://static-cdn.jtvnw.net/emoticons/v2/${emoteId}/default/dark/3.0`;

        for (let i = 0; i < count; i++) {
          if (urlsToSpawn.length >= MAX_EMOTES_PER_MSG) break;
          urlsToSpawn.push(emoteUrl);
        }
      }
    }

    if (urlsToSpawn.length > 0) {
      setLog(`スタンプ検知: 合計 ${urlsToSpawn.length} 個`);

      urlsToSpawn.forEach((url, index) => {
        if (CONFIG.staggerDelayMs > 0 && index > 0) {
          setTimeout(() => {
            spawnStamp(url);
          }, index * CONFIG.staggerDelayMs);
        } else {
          spawnStamp(url);
        }
      });
    }
  }

  /* ========================================================
   * Twitch Chat 接続（再接続タイマー排他制御・ゾンビ接続防止済み）
   * ======================================================== */
  let activeWs = null;
  let reconnectTimer = null;

  function scheduleReconnect(delayMs = 3000) {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectTwitchChat();
    }, delayMs);
  }

  function connectTwitchChat() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (activeWs) {
      try {
        activeWs.onclose = null;
        activeWs.onmessage = null;
        activeWs.onerror = null;
        activeWs.close();
      } catch (_) {}
      activeWs = null;
    }

    const channel = CONFIG.channel.trim().toLowerCase();
    if (!VALID_CHANNEL_RE.test(channel)) {
      connStatus.textContent = 'チャンネル名が不正です';
      connStatus.className = 'status-err';
      setLog('チャンネル名が不正です (英小文字・数字・_ 1〜25文字)');
      return;
    }

    connStatus.textContent = `#${channel} に接続試行中...`;
    connStatus.className = 'status-err';

    const ws = new WebSocket('wss://irc-ws.chat.twitch.tv:443');
    activeWs = ws;

    ws.onopen = () => {
      ws.send('CAP REQ :twitch.tv/tags twitch.tv/commands');
      ws.send('PASS SCHMOOPIE');
      ws.send(`NICK justinfan${Math.floor(Math.random() * 80000 + 1000)}`);
      ws.send(`JOIN #${channel}`);
    };

    ws.onmessage = (event) => {
      const raw = event.data;
      const lines = raw.split(/\r\n|\r|\n/);

      for (const line of lines) {
        if (!line) continue;

        if (line.startsWith('PING')) {
          ws.send('PONG :tmi.twitch.tv');
          continue;
        }

        if (line.includes(`JOIN #${channel}`) || line.includes(':End of /NAMES list')) {
          connStatus.textContent = `接続完了 (#${channel})`;
          connStatus.className = 'status-ok';
          setLog(`チャット待機中...`);
        }

        if (line.includes('PRIVMSG')) {
          const userMatch = /:([^!]+)!/.exec(line);
          const user = userMatch ? userMatch.at(1) : 'someone';
          setLog(`受信 [${user}]: スタンプ解析中...`);

          parseEmotesFromLine(line);
        }
      }
    };

    ws.onerror = () => {
      connStatus.textContent = '接続エラー';
      connStatus.className = 'status-err';
    };

    ws.onclose = () => {
      connStatus.textContent = '切断 (再接続中...)';
      connStatus.className = 'status-err';
      scheduleReconnect(3000);
    };
  }

  /* ========================================================
   * テスト発射機能
   * ======================================================== */
  const TEST_STAMPS = [
    'https://static-cdn.jtvnw.net/emoticons/v2/25/default/dark/3.0',
    'https://static-cdn.jtvnw.net/emoticons/v2/305954156/default/dark/3.0',
    'https://static-cdn.jtvnw.net/emoticons/v2/425618/default/dark/3.0'
  ];

  function triggerTest() {
    setLog('テスト連射実行 (3個)');
    TEST_STAMPS.forEach((url, idx) => {
      setTimeout(() => {
        spawnStamp(url);
      }, idx * CONFIG.staggerDelayMs);
    });
  }

  window.addEventListener('click', triggerTest);
  window.addEventListener('keydown', (e) => {
    if (e.key === 't' || e.key === 'T') triggerTest();
  });

  function resolveChannelName() {
    const paramCh = (new URLSearchParams(location.search).get('channel') || '').trim();
    const localCh = (window.OBS_LOCAL_ENV && window.OBS_LOCAL_ENV.TWITCH_CHANNEL)
      ? String(window.OBS_LOCAL_ENV.TWITCH_CHANNEL).trim()
      : '';
    return paramCh || localCh || '';
  }

  async function init() {
    const initial = resolveChannelName();
    if (initial) {
      CONFIG.channel = initial;
      connectTwitchChat();
      return;
    }

    const readyPromise = window.OBSWidgetsEnv && window.OBSWidgetsEnv.configReady;
    if (readyPromise && typeof readyPromise.then === 'function') {
      await Promise.race([
        readyPromise,
        new Promise((resolve) => setTimeout(resolve, 1500)),
      ]);
      const resolved = resolveChannelName();
      if (resolved) {
        CONFIG.channel = resolved;
        connectTwitchChat();
        return;
      }
      // タイムアウト後にサーバー応答が返ってチャンネルが確定した場合も追従して接続し直す
      readyPromise.then(() => {
        const late = resolveChannelName();
        if (late && late.toLowerCase() !== CONFIG.channel.trim().toLowerCase()) {
          CONFIG.channel = late;
          connectTwitchChat();
        }
      });
    }

    connectTwitchChat();
  }

  init();
})();
