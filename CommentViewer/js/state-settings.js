// ========================================================
// CommentViewer 状態管理・DOM参照・設定同期・重複排除 (js/state-settings.js)
// ========================================================
'use strict';

// file:// で直接開かれた場合でもローカルサーバー(localhost:8080)のAPIを使えるようにする
const API_BASE = (window.location.protocol === 'http:' || window.location.protocol === 'https:')
  ? ''
  : 'http://localhost:8080';
const VOICEVOX_DIRECT_URL = 'http://127.0.0.1:50021';

const stage = document.getElementById('viewer-stage');
const hudPanel = document.getElementById('hud-panel');
const btnHideHud = document.getElementById('btn-hide-hud');
const hudLog = document.getElementById('hud-log');

const dotIrc = document.getElementById('dot-irc');
const dotEventSub = document.getElementById('dot-eventsub');
const dotTranslate = document.getElementById('dot-translate');
const dotVoicevox = document.getElementById('dot-voicevox');
const scopeWarningBox = document.getElementById('scope-warning-box');
const btnOauthUpgrade = document.getElementById('btn-oauth-upgrade');

const chkTtsEnabled = document.getElementById('chk-tts-enabled');
const chkTranslateEnabled = document.getElementById('chk-translate-enabled');
const selSpeaker = document.getElementById('sel-speaker');
const btnReloadSpeakers = document.getElementById('btn-reload-speakers');
const rngSpeed = document.getElementById('rng-speed');
const lblSpeed = document.getElementById('lbl-speed');
const rngVolume = document.getElementById('rng-volume');
const lblVolume = document.getElementById('lbl-volume');
const chkReadUser = document.getElementById('chk-read-user');
const chkReadAlerts = document.getElementById('chk-read-alerts');
const btnClearFeed = document.getElementById('btn-clear-feed');
const selTheme = document.getElementById('sel-theme');

// URLパラメータ (?hud=0 なら初期状態で管理パネルを非表示, ?theme=comic / ?theme=default でスタイル指定)
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.get('hud') === '0' || urlParams.get('overlay') === '1') {
  hudPanel.classList.add('hidden');
}

// URLパラメータからテーマを取得（大文字小文字 ?Theme= / ?theme= / ?style= / ?Style= に完全対応）
function getUrlTheme() {
  for (const [k, v] of urlParams.entries()) {
    const key = k.trim().toLowerCase();
    if (key === 'theme' || key === 'style') {
      const val = String(v || '').trim().toLowerCase();
      if (['comic', 'sf6', 'bubble'].includes(val)) return 'comic';
      if (['default', 'standard', 'glass'].includes(val)) return 'default';
    }
  }
  return null;
}
const lockedUrlTheme = getUrlTheme();

// 状態管理（★デフォルトは必ず元のスタンダード 'default'）
// env-config.local.js または /api/config からチャンネル名が解決済みなら初期値として即座にセット
let channelName = (window.OBS_LOCAL_ENV && window.OBS_LOCAL_ENV.TWITCH_CHANNEL)
  || (window.OBS_SERVER_CONFIG && window.OBS_SERVER_CONFIG.channel_name)
  || '';
let settings = {
  theme: lockedUrlTheme || 'default', // デフォルトは元のスタンダード
  tts_enabled: true,
  speaker_id: 3,
  tts_speed: 1.05,
  tts_volume: 1.0,
  read_username: false,
  read_alerts: true,
  translate_enabled: true,
  max_comments: 8,
  hide_after_sec: 0
};

function applyTheme(themeName) {
  const effectiveTheme = (themeName === 'comic') ? 'comic' : 'default';
  settings.theme = effectiveTheme;
  document.body.classList.remove('theme-default', 'theme-comic');
  document.body.classList.add(`theme-${effectiveTheme}`);
  if (stage) {
    stage.classList.remove('theme-default', 'theme-comic');
    stage.classList.add(`theme-${effectiveTheme}`);
  }
  if (selTheme) {
    selTheme.value = effectiveTheme;
  }
}
applyTheme(settings.theme);

// 重複排除用セット（IRC と EventSub の両方で同じメッセージを受信した場合の二重処理防止）
const processedMessageIds = new Set();
function markProcessed(id) {
  if (!id) return false;
  if (processedMessageIds.has(id)) return true;
  processedMessageIds.add(id);
  if (processedMessageIds.size > 300) {
    const first = processedMessageIds.values().next().value;
    processedMessageIds.delete(first);
  }
  return false;
}

// BroadcastChannel
let controlChannel = null;
let authChannel = null;
try {
  controlChannel = new BroadcastChannel('obs_widgets_comment_viewer_v1');
  controlChannel.onmessage = (ev) => {
    const data = ev.data || {};
    if (data.type === 'update_settings' && data.settings) {
      applySettingsToUI(data.settings, false);
    } else if (data.type === 'test_trigger' && data.testKind) {
      runTestTrigger(data.testKind);
    } else if (data.type === 'clear_feed') {
      stage.innerHTML = '';
    }
  };
  authChannel = new BroadcastChannel('obs_widgets_twitch_auth_v1');
  authChannel.onmessage = (ev) => {
    if (ev.data && ev.data.type === 'auth_updated') {
      setLog('🔑 新しいトークンを検知！EventSubを再接続します...');
      initViewer();
    }
  };
} catch (e) {}

function setLog(text) {
  hudLog.textContent = text;
}

// Hキーでパネル表示切替
window.addEventListener('keydown', (e) => {
  if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT')) return;
  if (e.key === 'h' || e.key === 'H') {
    hudPanel.classList.toggle('hidden');
  }
});
btnHideHud.addEventListener('click', () => hudPanel.classList.add('hidden'));

/* ========================================================
 * 設定の同期・保存
 * ======================================================== */
function applySettingsToUI(newSettings, saveRemote = true) {
  settings = Object.assign({}, settings, newSettings);

  // ★重要: URLパラメータでテーマが指定されている場合は、localStorageやサーバー設定による上書きをブロックする！
  if (lockedUrlTheme) {
    settings.theme = lockedUrlTheme;
  } else if (newSettings && newSettings.theme !== undefined) {
    settings.theme = (newSettings.theme === 'comic') ? 'comic' : 'default';
  } else if (!settings.theme) {
    settings.theme = 'default';
  }

  applyTheme(settings.theme);

  chkTtsEnabled.checked = Boolean(settings.tts_enabled);
  chkTranslateEnabled.checked = Boolean(settings.translate_enabled);
  if (settings.speaker_id !== undefined) {
    selSpeaker.value = String(settings.speaker_id);
  }
  rngSpeed.value = String(settings.tts_speed ?? 1.05);
  lblSpeed.textContent = Number(settings.tts_speed ?? 1.05).toFixed(2);
  rngVolume.value = String(settings.tts_volume ?? 1.0);
  lblVolume.textContent = Number(settings.tts_volume ?? 1.0).toFixed(1);
  chkReadUser.checked = Boolean(settings.read_username);
  chkReadAlerts.checked = Boolean(settings.read_alerts);

  try {
    localStorage.setItem('obs_comment_viewer_settings_v1', JSON.stringify(settings));
  } catch (e) {}

  if (saveRemote) {
    authFetch(`${API_BASE}/api/twitch/save-config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ viewer_settings: settings })
    }).catch(() => {});
    if (controlChannel) {
      controlChannel.postMessage({ type: 'settings_synced', settings });
    }
  }
}

function onSettingInputChanged() {
  const chosenTheme = selTheme ? selTheme.value : settings.theme;
  try {
    localStorage.setItem('theme_explicitly_selected', '1');
  } catch (e) {}

  applySettingsToUI({
    theme: chosenTheme,
    tts_enabled: chkTtsEnabled.checked,
    translate_enabled: chkTranslateEnabled.checked,
    speaker_id: parseInt(selSpeaker.value, 10) || 3,
    tts_speed: parseFloat(rngSpeed.value) || 1.05,
    tts_volume: parseFloat(rngVolume.value) || 1.0,
    read_username: chkReadUser.checked,
    read_alerts: chkReadAlerts.checked
  }, true);
}

[selTheme, chkTtsEnabled, chkTranslateEnabled, selSpeaker, rngSpeed, rngVolume, chkReadUser, chkReadAlerts].filter(Boolean).forEach(el => {
  el.addEventListener('change', onSettingInputChanged);
});
rngSpeed.addEventListener('input', () => { lblSpeed.textContent = Number(rngSpeed.value).toFixed(2); });
rngVolume.addEventListener('input', () => { lblVolume.textContent = Number(rngVolume.value).toFixed(1); });
