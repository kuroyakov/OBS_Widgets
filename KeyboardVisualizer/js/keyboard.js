/* ========================================================
 * OBS ＆ 音量監視設定（接続先IPはアクセス元ホストから自動解決）
 * ======================================================== */
const wsHost = (location.hostname && location.hostname !== '') ? location.hostname : 'localhost';
const OBS_CONFIG = {
  // HTTPS ページでは env-config が返すトンネル経由のアドレスを使う (Issue #115)
  address: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_DEFAULT_ADDRESS) || `ws://${wsHost}:4455`,
  password: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_DEFAULT_PASSWORD) || '',
  bgmSource: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_MEDIA_SOURCES && window.OBSWidgetsEnv.OBS_MEDIA_SOURCES.bgm) || 'BGM',
  filterFadeOut: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_FILTER_NAMES && window.OBSWidgetsEnv.OBS_FILTER_NAMES.bgmFadeOut) || 'Fade Out',
  filterFadeIn: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_FILTER_NAMES && window.OBSWidgetsEnv.OBS_FILTER_NAMES.bgmFadeIn) || 'Fade In',
  monitorAudioSource: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_AUDIO_SOURCES && window.OBSWidgetsEnv.OBS_AUDIO_SOURCES.desktop) || 'Desktop Audio',
  silenceDbThreshold: -45, // 無音判定しきい値 (dB)
  silenceDuration: 5000    // 無音継続時間 (ms)
};

(() => {
  'use strict';

  const keyboardWrapper = document.getElementById('keyboard-wrapper');
  const statusBox = document.getElementById('midi-status');
  const runStatus = document.getElementById('run-status');
  const midiInfo = document.getElementById('midi-info');
  const obsInfo = document.getElementById('obs-info');
  const bgmInfo = document.getElementById('bgm-info');
  const meterInfo = document.getElementById('meter-info');
  const noteMonitor = document.getElementById('note-monitor');

  const whiteKeysContainer = document.getElementById('white-keys');
  const blackKeysContainer = document.getElementById('black-keys');

  /* ========================================================
   * ★ 動作状態フラグ（Sキー / コントローラー遠隔操作でトグル） ★
   * ======================================================== */
  let isSuspended = false;

  let midiBroadcastChannel = null;
  try {
    midiBroadcastChannel = new BroadcastChannel('keyboard_visualizer_channel_v1');
  } catch (e) {}

  function broadcastKeyboardState() {
    const hudVisible = !statusBox.classList.contains('hidden');
    const statePayload = {
      type: 'KEYBOARD_STATE',
      suspended: Boolean(isSuspended),
      hudVisible
    };
    if (isObsConnected && obs) {
      obs.call('BroadcastCustomEvent', { eventData: statePayload }).catch(() => {});
    }
    if (midiBroadcastChannel) {
      try {
        midiBroadcastChannel.postMessage(statePayload);
      } catch (e) {}
    }
  }

  function setSuspendState(nextSuspended) {
    isSuspended = Boolean(nextSuspended);

    if (isSuspended) {
      // --- 停止時の処理 ---
      keyboardWrapper.classList.add('suspended');
      runStatus.textContent = '停止中 (PAUSED)';
      runStatus.className = 'status-pause';
      noteMonitor.textContent = '※Sキーまたはコントローラーで再開します';

      // 点灯中の鍵盤をすべて消灯
      allNotesOff();

      // フェード中だった場合は安全のためにBGMを通常音量に戻す
      if (isBgmDucked) {
        isBgmDucked = false;
        setObsFilter(OBS_CONFIG.filterFadeIn, OBS_CONFIG.filterFadeOut);
      }

      bgmInfo.textContent = '停止中 (待機)';
      bgmInfo.style.color = '#94a3b8';
      meterInfo.textContent = '監視停止中';
      meterInfo.style.color = '#94a3b8';

      // タイマー・打鍵履歴の完全クリア
      noteTimestamps = [];
      emitMidiViewEvent({ action: 'suspend', suspended: true });
    } else {
      // --- 再開時の処理 ---
      keyboardWrapper.classList.remove('suspended');
      runStatus.textContent = '稼働中 (ACTIVE)';
      runStatus.className = 'status-ok';
      noteMonitor.textContent = '鍵盤を弾いてください';
      bgmInfo.textContent = '通常音量';
      bgmInfo.style.color = '#38bdf8';
      lastSoundTime = Date.now();
      noteTimestamps = [];
      emitMidiViewEvent({ action: 'suspend', suspended: false });
    }

    broadcastKeyboardState();
    syncMeterSubscription();
  }

  function toggleSuspend() {
    setSuspendState(!isSuspended);
  }

  function setHudVisibleState(visible) {
    if (visible) {
      statusBox.classList.remove('hidden');
    } else {
      statusBox.classList.add('hidden');
    }
    broadcastKeyboardState();
  }

  function toggleHudVisible() {
    setHudVisibleState(statusBox.classList.contains('hidden'));
  }

  function handleRemoteKeyboardControl(data) {
    if (!data || data.type !== 'KEYBOARD_CONTROL') return;
    if (data.action === 'toggleSuspend') {
      toggleSuspend();
    } else if (data.action === 'setSuspend' && typeof data.suspended === 'boolean') {
      setSuspendState(data.suspended);
    } else if (data.action === 'toggleHud') {
      toggleHudVisible();
    } else if (data.action === 'setHud' && typeof data.hudVisible === 'boolean') {
      setHudVisibleState(data.hudVisible);
    } else if (data.action === 'requestState') {
      broadcastKeyboardState();
    }
  }

  if (midiBroadcastChannel) {
    midiBroadcastChannel.onmessage = (ev) => {
      handleRemoteKeyboardControl(ev.data);
    };
  }

  // キーボード操作リスナー
  window.addEventListener('keydown', (e) => {
    // Hキー: ステータス表示切替
    if (e.key === 'h' || e.key === 'H') {
      toggleHudVisible();
    }
    // Sキー: プログラム全体の停止/再開
    if (e.key === 's' || e.key === 'S') {
      toggleSuspend();
    }
  });

  /* ========================================================
   * OBS WebSocket 接続 & 音量監視 (shared/obs-client.js 共通ラッパー使用)
   * ======================================================== */
  let obs = null;
  let isObsConnected = false;

  let isBgmDucked = false;
  let noteTimestamps = [];
  let lastSoundTime = Date.now();

  // 購読セット (Issue #143 / P-05): 65536 = InputVolumeMeters
  const EVENT_SUBSCRIPTIONS_FULL = (1023 | 65536);
  const EVENT_SUBSCRIPTIONS_NO_METERS = 1023;

  const obsClient = window.OBSWidgetsClient.createManagedObsClient({
    addresses: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_FALLBACK_ADDRESSES) || [OBS_CONFIG.address],
    password: OBS_CONFIG.password,
    eventSubscriptions: EVENT_SUBSCRIPTIONS_FULL,
    reconnectDelayMs: 3500,
    setupListeners: (instance) => {
      instance.on('InputVolumeMeters', handleVolumeMeters);
      instance.on('CustomEvent', (raw) => {
        const data = (raw && raw.eventData) || raw;
        handleRemoteKeyboardControl(data);
      });
    },
    onConnected: (instance) => {
      obs = instance;
      isObsConnected = true;
      obsInfo.textContent = '接続完了 (音量監視中)';
      obsInfo.className = 'status-ok';
      broadcastKeyboardState();
      syncMeterSubscription();
    },
    onDisconnected: (err) => {
      isObsConnected = false;
      obsInfo.textContent = err && err.message ? `切断/再接続中 (${err.message})` : '切断 (再接続中...)';
      obsInfo.className = 'status-err';
    }
  });

  // 停止/稼働状態に合わせて InputVolumeMeters の購読を付け替える (Issue #143 / P-05)。
  // 停止中はメーターを使う処理が存在せず、OBS 側の毎秒約20回の送信も止まる。
  // 再接続時は接続オプションの購読 (FULL) で Identify されるため、onConnected からも呼んで状態に揃える。
  async function syncMeterSubscription() {
    if (!isObsConnected || !obs) return;
    const eventSubscriptions = isSuspended ? EVENT_SUBSCRIPTIONS_NO_METERS : EVENT_SUBSCRIPTIONS_FULL;
    try {
      await obs.reidentify({ eventSubscriptions });
    } catch (_) {}
  }

  function connectOBS() {
    obsClient.connect();
  }

  async function setObsFilter(filterToEnable, filterToDisable) {
    if (!isObsConnected || !obs) return;

    try {
      if (filterToDisable) {
        await obs.call('SetSourceFilterEnabled', {
          sourceName: OBS_CONFIG.bgmSource,
          filterName: filterToDisable,
          filterEnabled: false
        }).catch(() => {});
      }

      await obs.call('SetSourceFilterEnabled', {
        sourceName: OBS_CONFIG.bgmSource,
        filterName: filterToEnable,
        filterEnabled: true
      });
    } catch (err) {
      console.error('OBS Filter Error:', err);
    }
  }

  /* ========================================================
   * 音量メーター解析（停止中は完全スキップ）
   * ======================================================== */
  function handleVolumeMeters(data) {
    // ★ Sキーで停止中は音量監視を完全にスルー ★
    if (isSuspended) return;

    if (!data?.inputs || !Array.isArray(data.inputs)) return;

    const target = data.inputs.find(item => item.inputName === OBS_CONFIG.monitorAudioSource);
    if (!target?.inputLevelsMul) return;

    let maxPeak = 0;
    target.inputLevelsMul.forEach(ch => {
      if (Array.isArray(ch) && ch.length > 0) {
        const peak = ch.at(0);
        if (peak > maxPeak) maxPeak = peak;
      }
    });

    const currentDb = maxPeak > 0 ? (20 * Math.log10(maxPeak)) : -100;
    const now = Date.now();

    if (currentDb > OBS_CONFIG.silenceDbThreshold) {
      lastSoundTime = now;
      meterInfo.textContent = `${currentDb.toFixed(1)} dB (発音中)`;
      meterInfo.style.color = '#4ade80';
    } else {
      const silenceDurationMs = now - lastSoundTime;
      const silenceSeconds = (silenceDurationMs / 1000).toFixed(1);

      meterInfo.textContent = `${currentDb.toFixed(1)} dB (無音: ${silenceSeconds}s)`;
      meterInfo.style.color = '#a78bfa';

      if (isBgmDucked && silenceDurationMs >= OBS_CONFIG.silenceDuration) {
        isBgmDucked = false;
        bgmInfo.textContent = '通常音量 (無音5秒検知)';
        bgmInfo.style.color = '#38bdf8';
        setObsFilter(OBS_CONFIG.filterFadeIn, OBS_CONFIG.filterFadeOut);
      }
    }
  }

  /* ========================================================
   * MIDI演奏検知（停止中は完全スキップ）
   * ======================================================== */
  function onNoteTriggered() {
    if (isSuspended) return;

    const now = Date.now();
    noteTimestamps.push(now);
    noteTimestamps = noteTimestamps.filter(t => now - t <= 5000);

    if (noteTimestamps.length >= 5 && !isBgmDucked) {
      isBgmDucked = true;
      lastSoundTime = now;
      bgmInfo.textContent = 'フェードアウト中 (演奏開始)';
      bgmInfo.style.color = '#f43f5e';
      setObsFilter(OBS_CONFIG.filterFadeOut, OBS_CONFIG.filterFadeIn);
    }
  }

  /* ========================================================
   * 88鍵盤のDOM生成 & マッピング
   * ======================================================== */
  const keyElements = new Map();
  const START_NOTE = 21;  // A0
  const END_NOTE = 108;   // C8
  const TOTAL_WHITE_KEYS = 52;

  function isBlackNote(note) {
    const n = note % 12;
    return n === 1 || n === 3 || n === 6 || n === 8 || n === 10;
  }

  function getBlackKeyOffsetRatio(note) {
    const n = note % 12;
    if (n === 1) return -0.13;
    if (n === 3) return  0.13;
    if (n === 6) return -0.14;
    if (n === 8) return -0.01;
    if (n === 10) return 0.14;
    return 0;
  }

  let whiteKeyIndex = 0;
  const oneWhiteKeyPercent = 100 / TOTAL_WHITE_KEYS;

  for (let note = START_NOTE; note <= END_NOTE; note++) {
    if (!isBlackNote(note)) {
      const key = document.createElement('div');
      key.className = 'key-white';
      key.dataset.note = note;
      whiteKeysContainer.appendChild(key);
      keyElements.set(note, key);
      whiteKeyIndex++;
    } else {
      const key = document.createElement('div');
      key.className = 'key-black';
      key.dataset.note = note;

      const offsetRatio = getBlackKeyOffsetRatio(note);
      const leftPercent = (whiteKeyIndex + offsetRatio) * oneWhiteKeyPercent;
      key.style.left = `${leftPercent}%`;

      blackKeysContainer.appendChild(key);
      keyElements.set(note, key);
    }
  }

  /* ========================================================
   * ★ OBS ブラウザソース (viewer.html) へのリアルタイム同期送信 ★
   * ======================================================== */
  function emitMidiViewEvent(payload) {
    const eventData = Object.assign({ type: 'KEYBOARD_MIDI' }, payload);

    // 1. OBS WebSocket (BroadcastCustomEvent) 経由で OBS ブラウザソースへ即時送信
    if (isObsConnected && obs) {
      obs.call('BroadcastCustomEvent', { eventData }).catch(() => {});
    }

    // 2. BroadcastChannel でも送信（同一ブラウザ確認用）
    if (midiBroadcastChannel) {
      try {
        midiBroadcastChannel.postMessage(eventData);
      } catch (e) {}
    }
  }

  function noteOn(noteNumber) {
    if (isSuspended) return; // 停止中は光らせない
    const el = keyElements.get(noteNumber);
    if (el) el.classList.add('active');
    emitMidiViewEvent({ action: 'noteOn', note: noteNumber });
    onNoteTriggered();
  }

  function noteOff(noteNumber) {
    if (isSuspended) return;
    const el = keyElements.get(noteNumber);
    if (el) el.classList.remove('active');
    emitMidiViewEvent({ action: 'noteOff', note: noteNumber });
  }

  function allNotesOff() {
    keyElements.forEach(el => el.classList.remove('active'));
    emitMidiViewEvent({ action: 'allNotesOff' });
  }

  // マウスクリックでもテスト発火できるように対応（実機オフ時の動作確認用）
  keyElements.forEach((el, note) => {
    el.addEventListener('mousedown', (e) => {
      e.preventDefault();
      noteMonitor.textContent = `▶ Test NoteOn: ${note}`;
      noteOn(note);
    });
    el.addEventListener('mouseup', () => {
      noteMonitor.textContent = `■ Test NoteOff: ${note}`;
      noteOff(note);
    });
    el.addEventListener('mouseleave', () => {
      if (el.classList.contains('active')) {
        noteOff(note);
      }
    });
  });

  /* ========================================================
   * MIDIメッセージ受信ハンドラ（停止中は無視）
   * ======================================================== */
  function handleMIDIMessage(event) {
    // ★ Sキーで停止中はMIDI処理を完全スルー ★
    if (isSuspended) return;

    if (!event.data || event.data.length < 2) return;

    const statusByte = event.data.at(0);
    const data1 = event.data.at(1);
    const data2 = event.data.length > 2 ? event.data.at(2) : 0;
    const messageType = statusByte & 0xF0;

    if (messageType === 0x90 && data2 > 0) {
      noteMonitor.textContent = `▶ NoteOn: ${data1} (Vel: ${data2})`;
      noteOn(data1);
      return;
    }

    if (messageType === 0x80 || (messageType === 0x90 && data2 === 0)) {
      noteMonitor.textContent = `■ NoteOff: ${data1}`;
      noteOff(data1);
      return;
    }

    if (messageType === 0xC0) {
      noteMonitor.textContent = `🔄 ProgramChange: ${data1} (全消灯)`;
      allNotesOff();
      return;
    }

    if (messageType === 0xB0) {
      if (data1 === 120 || data1 === 123 || data1 === 121) {
        noteMonitor.textContent = `🛑 Reset: CC${data1} (全消灯)`;
        allNotesOff();
      }
    }
  }

  /* ========================================================
   * Web MIDI 接続
   * ======================================================== */
  function onMIDISuccess(midiAccess) {
    const inputs = Array.from(midiAccess.inputs.values());

    if (inputs.length === 0) {
      midiInfo.textContent = '機器未検出';
    } else {
      midiInfo.textContent = `接続中 (${inputs.length}ポート)`;
      midiInfo.className = 'status-ok';
      inputs.forEach(input => {
        input.onmidimessage = handleMIDIMessage;
      });
    }

    midiAccess.onstatechange = (e) => {
      if (e.port.type === 'input') onMIDISuccess(midiAccess);
    };
  }

  function onMIDIFailure(err) {
    midiInfo.textContent = 'アクセスエラー';
  }

  if (navigator.requestMIDIAccess) {
    navigator.requestMIDIAccess({ sysex: false }).then(onMIDISuccess, onMIDIFailure);
  } else {
    midiInfo.textContent = '非対応ブラウザ';
  }

  connectOBS();

  /* ========================================================
   * ローカルサーバー死活監視ハートビート送信 (4秒周期)
   * ======================================================== */
  function sendKeyboardHeartbeat() {
    const apiBase = (window.OBSWidgetsEnv && typeof window.OBSWidgetsEnv.API_BASE === 'string')
      ? window.OBSWidgetsEnv.API_BASE
      : ((location.protocol === 'http:' || location.protocol === 'https:')
        ? ''
        : 'http://localhost:8080');
    const hbUrl = `${apiBase}/api/heartbeat?client=keyboard`;
    fetch(hbUrl, { cache: 'no-store' }).catch(() => {});
  }
  setInterval(sendKeyboardHeartbeat, 4000);
  sendKeyboardHeartbeat();

})();
