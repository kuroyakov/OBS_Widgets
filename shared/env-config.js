/**
 * OBS Widgets 共通環境・定数定義モジュール (/shared/env-config.js)
 * - 接続先ホストの自動判別（Mac ローカル / iPad Safari LAN 経由 / file:// のすべてに対応）
 * - OBS WebSocket 接続情報・シーン名・ソース名・BroadcastChannel 名の一元管理
 * - Git管理ファイルには機密情報（実パスワード等）をハードコードせず、env-config.local.js または /api/config から解決
 * - OBS CEF のメインスレッドフリーズを防止するため、同期 XMLHttpRequest (async=false) を使用せず非同期で補完
 */
(() => {
  'use strict';

  const localEnv = (typeof window !== 'undefined' && window.OBS_LOCAL_ENV) || {};
  const serverCfg = (typeof window !== 'undefined' && window.OBS_SERVER_CONFIG) || {};

  const WS_HOST = localEnv.WS_HOST || ((typeof location !== 'undefined' && location.hostname && location.hostname !== '')
    ? location.hostname
    : 'localhost');

  const OBS_DEFAULT_PORT = localEnv.OBS_DEFAULT_PORT || 4455;

  // HTTPS ページからは平文 ws:// へ接続できない (混合コンテンツ) ため、
  // HTTPS で開かれたページは TLS リスナーの WebSocket トンネル (/obs-ws) を
  // 経由して OBS WebSocket へ接続する (Issue #115)。location.host は
  // ポート (8443) を含むため、そのままトンネルの接続先になる。
  const isHttpsPage = (typeof location !== 'undefined' && location.protocol === 'https:');
  const OBS_TUNNEL_ADDRESS = isHttpsPage ? `wss://${location.host}/obs-ws` : null;

  const OBS_DEFAULT_ADDRESS = OBS_TUNNEL_ADDRESS || `ws://${WS_HOST}:${OBS_DEFAULT_PORT}`;

  // アクセス元（Macローカル or iPad等LAN経由）に応じた最適なフォールバック順序
  // ※ iPad (LAN経由) の場合は 127.0.0.1 / localhost は iPad 自身を指してしまうため絶対に候補に含めず、
  //    現在アクセス中のホスト名と Mac の有効な LAN IP (有線 en0 / Wi-Fi en1 等) のみを候補にする。
  // ※ トンネル利用時は候補はトンネル1本のみ (平文 ws:// の候補は遮断されるだけなので含めない)。
  const isLocalHost = (WS_HOST === 'localhost' || WS_HOST === '127.0.0.1' || WS_HOST === '::1');
  const serverLanAddrs = Array.isArray(serverCfg.lan_ips)
    ? serverCfg.lan_ips.map(ip => `ws://${ip}:${OBS_DEFAULT_PORT}`)
    : [];

  const OBS_FALLBACK_ADDRESSES = OBS_TUNNEL_ADDRESS
    ? [OBS_TUNNEL_ADDRESS]
    : isLocalHost
    ? [`ws://127.0.0.1:${OBS_DEFAULT_PORT}`, `ws://localhost:${OBS_DEFAULT_PORT}`]
    : Array.from(new Set([
        OBS_DEFAULT_ADDRESS,
        ...serverLanAddrs
      ]));

  // OBS WebSocket パスワード（Git管理上は空文字。ローカルは env-config.local.js / twitch_config.json / localStorage から自動反映）
  const OBS_DEFAULT_PASSWORD = localEnv.OBS_DEFAULT_PASSWORD
    || serverCfg.obs_password
    || (typeof localStorage !== 'undefined' && localStorage.getItem('obs_password'))
    || '';

  function resolveServerPort(raw) {
    if (raw === undefined || raw === null || raw === '') return 8080;
    const str = String(raw).trim();
    if (!str) return 8080;
    if (/^[0-9]+$/.test(str)) {
      const n = Number(str);
      if (Number.isInteger(n) && n >= 1 && n <= 65535) return n;
    }
    console.warn(`[env-config] Invalid OBS_LOCAL_ENV.SERVER_PORT (${raw}); falling back to 8080`);
    return 8080;
  }

  const SERVER_PORT = resolveServerPort(localEnv.SERVER_PORT);

  const API_BASE = (typeof location !== 'undefined' && (location.protocol === 'http:' || location.protocol === 'https:'))
    ? ''
    : `http://localhost:${SERVER_PORT}`;

  const VOICEVOX_DIRECT_URL = 'http://127.0.0.1:50021';

  const PNGTUBER_SOURCE_NAME = localEnv.PNGTUBER_SOURCE_NAME
    || serverCfg.pngtuber_source_name
    || 'pngtuber';

  // シーン名・ソース名・フィルタ名の既定値。自分の OBS 構成に合わせて
  // env-config.local.js の OBS_LOCAL_ENV で同名キーを指定すると上書きできます。
  const OBS_SCENE_NAMES = Object.assign({
    starting: 'Starting',
    liveMain: 'Live Main',
    liveChat: 'Live Chat',
    ending:   'Ending'
  }, localEnv.OBS_SCENE_NAMES || {});

  const OBS_MEDIA_SOURCES = Object.assign({
    intro: 'Intro BGM',
    bgm:   'BGM',
    outro: 'Outro BGM',
    keyboardVisualizer: 'KeyboardVisualizer',
    pngtuber: PNGTUBER_SOURCE_NAME
  }, localEnv.OBS_MEDIA_SOURCES || {});

  const OBS_FILTER_NAMES = Object.assign({
    introMove:  'Move Value',
    bgmFadeIn:  'Fade In',
    bgmFadeOut: 'Fade Out'
  }, localEnv.OBS_FILTER_NAMES || {});

  const OBS_AUDIO_SOURCES = Object.assign({
    desktop: 'Desktop Audio'
  }, localEnv.OBS_AUDIO_SOURCES || {});

  const BROADCAST_CHANNELS = {
    ticker:        'stream_ticker_broadcast_v1',
    commentViewer: 'obs_widgets_comment_viewer_v1',
    twitchAuth:    'obs_widgets_twitch_auth_v1',
    keyboardMidi:  'keyboard_visualizer_channel_v1',
    pngtuber:      'pngtuber'
  };

  function isSafeHttpUrl(url) {
    return typeof url === 'string' && /^https?:\/\//i.test(url.trim());
  }

  function isPlainObject(val) {
    return Boolean(val) && typeof val === 'object' && !Array.isArray(val);
  }

  function mergeObsNames(target, serverMap, localMap) {
    if (!isPlainObject(serverMap)) return;
    const explicitLocal = isPlainObject(localMap) ? localMap : {};
    for (const key of Object.keys(serverMap)) {
      if (key === '__proto__' || key === 'constructor' || key === 'prototype') continue;
      if (Object.prototype.hasOwnProperty.call(explicitLocal, key)) continue;
      const val = serverMap[key];
      if (typeof val === 'string') {
        target[key] = val;
      }
    }
  }

  let resolveConfigReady = () => {};
  const configReady = new Promise((resolve) => {
    resolveConfigReady = resolve;
  });

  window.OBSWidgetsEnv = {
    WS_HOST,
    OBS_DEFAULT_PORT,
    OBS_DEFAULT_ADDRESS,
    OBS_TUNNEL_ADDRESS,
    OBS_FALLBACK_ADDRESSES,
    OBS_DEFAULT_PASSWORD,
    SERVER_PORT,
    API_BASE,
    VOICEVOX_DIRECT_URL,
    PNGTUBER_SOURCE_NAME,
    OBS_SCENE_NAMES,
    OBS_MEDIA_SOURCES,
    OBS_FILTER_NAMES,
    OBS_AUDIO_SOURCES,
    BROADCAST_CHANNELS,
    isSafeHttpUrl,
    configReady,
    configStatus: null
  };

  // 非同期で /api/config からサーバー設定・LAN IP一覧を補完（CEFメインスレッドを一切ブロックしない）
  if (typeof window !== 'undefined' && typeof fetch === 'function') {
    const apiUrl = `${API_BASE}/api/config`;
    const doFetch = (typeof window.authFetch === 'function') ? window.authFetch : fetch;
    doFetch(apiUrl, { cache: 'no-store' })
      .then(res => {
        if (res && (res.status === 401 || res.status === 403)) {
          window.OBSWidgetsEnv.configStatus = 'unauthorized';
          return null;
        }
        if (!res || !res.ok) {
          window.OBSWidgetsEnv.configStatus = 'error';
          return null;
        }
        return res.json().catch(() => {
          window.OBSWidgetsEnv.configStatus = 'error';
          return null;
        });
      })
      .then(sData => {
        if (!sData || typeof sData !== 'object') {
          if (window.OBSWidgetsEnv.configStatus === null) {
            window.OBSWidgetsEnv.configStatus = 'error';
          }
          return;
        }
        const hasAuthObsNames =
          isPlainObject(sData.obs_scene_names) ||
          isPlainObject(sData.obs_media_sources) ||
          isPlainObject(sData.obs_filter_names) ||
          isPlainObject(sData.obs_audio_sources);
        window.OBSWidgetsEnv.configStatus = hasAuthObsNames ? 'ok' : 'unauthorized';

        window.OBS_SERVER_CONFIG = sData;
        window.OBS_LOCAL_ENV = window.OBS_LOCAL_ENV || {};
        if (sData.obs_password && !window.OBS_LOCAL_ENV.OBS_DEFAULT_PASSWORD) {
          window.OBS_LOCAL_ENV.OBS_DEFAULT_PASSWORD = sData.obs_password;
          window.OBSWidgetsEnv.OBS_DEFAULT_PASSWORD = sData.obs_password;
        }
        if (sData.channel_name && !window.OBS_LOCAL_ENV.TWITCH_CHANNEL) {
          window.OBS_LOCAL_ENV.TWITCH_CHANNEL = sData.channel_name;
        }
        if (sData.channel_name && !window.OBS_LOCAL_ENV.STREAMER_NAME) {
          window.OBS_LOCAL_ENV.STREAMER_NAME = sData.channel_name;
        }
        if (sData.pngtuber_source_name && !window.OBS_LOCAL_ENV.PNGTUBER_SOURCE_NAME) {
          window.OBS_LOCAL_ENV.PNGTUBER_SOURCE_NAME = sData.pngtuber_source_name;
          window.OBSWidgetsEnv.PNGTUBER_SOURCE_NAME = sData.pngtuber_source_name;
        }
        mergeObsNames(
          OBS_SCENE_NAMES,
          sData.obs_scene_names,
          window.OBS_LOCAL_ENV.OBS_SCENE_NAMES || localEnv.OBS_SCENE_NAMES
        );
        mergeObsNames(
          OBS_MEDIA_SOURCES,
          sData.obs_media_sources,
          window.OBS_LOCAL_ENV.OBS_MEDIA_SOURCES || localEnv.OBS_MEDIA_SOURCES
        );
        mergeObsNames(
          OBS_FILTER_NAMES,
          sData.obs_filter_names,
          window.OBS_LOCAL_ENV.OBS_FILTER_NAMES || localEnv.OBS_FILTER_NAMES
        );
        mergeObsNames(
          OBS_AUDIO_SOURCES,
          sData.obs_audio_sources,
          window.OBS_LOCAL_ENV.OBS_AUDIO_SOURCES || localEnv.OBS_AUDIO_SOURCES
        );
        if (!isLocalHost && !OBS_TUNNEL_ADDRESS && Array.isArray(sData.lan_ips)) {
          for (const ip of sData.lan_ips) {
            const candidate = `ws://${ip}:${OBS_DEFAULT_PORT}`;
            if (ip && !OBS_FALLBACK_ADDRESSES.includes(candidate)) {
              OBS_FALLBACK_ADDRESSES.push(candidate);
            }
          }
        }
      })
      .catch(() => {
        window.OBSWidgetsEnv.configStatus = 'error';
      })
      .finally(() => resolveConfigReady(window.OBS_SERVER_CONFIG || null));
  } else {
    resolveConfigReady(null);
  }
})();
