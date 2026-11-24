/**
 * PNGTuber 設定ページ制御スクリプト (PngTuber/js/settings.js)
 * - アバター（メイン＋avatars）ごとの設定の読み込み・マージ保存 (GET/POST /api/pngtuber/config)
 * - 4枚の画像プレビュー・既存ファイル選択 (GET /api/pngtuber/images)・PNG アップロード (POST /api/pngtuber/image/<slot>)
 * - SSE (GET /api/pngtuber/stream) を使ったライブレベルメーターと open/close しきい値調整
 * - OBS 音声入力一覧のドロップダウン選択 (GET /api/pngtuber/inputs) と未接続時の文字列入力フォールバック
 */
(() => {
  'use strict';

  const SLOTS = [
    'mouth_open_eye_open',
    'mouth_open_eye_closed',
    'mouth_closed_eye_open',
    'mouth_closed_eye_closed',
  ];

  const DB_MIN = -60;
  const DB_MAX = 0;
  const LINEAR_MIN = 0.001; // 10^(-60/20)
  const LINEAR_MAX = 1.0;   // 10^(0/20)
  const DEFAULT_OPEN_LINEAR = 0.05;
  const DEFAULT_CLOSE_LINEAR = 0.025;

  function resolveApiBase() {
    if (window.OBSWidgetsEnv && typeof window.OBSWidgetsEnv.API_BASE === 'string') {
      return window.OBSWidgetsEnv.API_BASE;
    }
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      return '';
    }
    return 'http://localhost:8080';
  }

  function apiFetch(url, options) {
    if (typeof window.authFetch === 'function') {
      return window.authFetch(url, options);
    }
    return fetch(url, options);
  }

  function clamp(num, min, max, fallback) {
    if (num === '' || num === null || num === undefined) return fallback;
    if (typeof num === 'string' && num.trim() === '') return fallback;
    const n = Number(num);
    if (!Number.isFinite(n)) return fallback;
    return Math.max(min, Math.min(max, n));
  }

  function round1(num) {
    const rounded = Math.round(num * 10) / 10;
    return rounded === 0 ? 0 : rounded;
  }

  function round6(num) {
    return Math.round(num * 1000000) / 1000000;
  }

  function linearToDb(linear) {
    const n = Number(linear);
    if (!Number.isFinite(n) || n <= 0) return -Infinity;
    return 20 * Math.log10(n);
  }

  function linearToClampedDb(linear, fallbackLinear) {
    const safe = clamp(linear, 0, LINEAR_MAX, fallbackLinear);
    if (safe <= LINEAR_MIN) return DB_MIN;
    return round1(clamp(linearToDb(safe), DB_MIN, DB_MAX, DB_MIN));
  }

  function dbToLinear(db) {
    const clamped = clamp(db, DB_MIN, DB_MAX, DB_MIN);
    return clamp(round6(Math.pow(10, clamped / 20)), LINEAR_MIN, LINEAR_MAX, LINEAR_MIN);
  }

  function dbToMeterPercent(db) {
    if (!Number.isFinite(db)) return 0;
    return clamp(((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100, 0, 100, 0);
  }

  function formatLevelDb(linear) {
    if (!Number.isFinite(linear) || linear <= 0) return '-∞ dB';
    const db = round1(linearToDb(linear));
    return `${db.toFixed(1)} dB`;
  }

  function resolveConfigThresholdsDb(cfg) {
    const openDb = linearToClampedDb(cfg && cfg.open_threshold, DEFAULT_OPEN_LINEAR);
    const defaultCloseDb = linearToClampedDb(DEFAULT_CLOSE_LINEAR, DEFAULT_CLOSE_LINEAR);
    const fallbackCloseDb = Math.min(openDb, defaultCloseDb);
    const rawClose = cfg && cfg.close_threshold;
    const isMissingClose = rawClose === '' || rawClose === null || rawClose === undefined
      || (typeof rawClose === 'string' && rawClose.trim() === '')
      || !Number.isFinite(Number(rawClose));
    const closeDb = isMissingClose
      ? fallbackCloseDb
      : Math.min(openDb, linearToClampedDb(rawClose, DEFAULT_CLOSE_LINEAR));
    return { openDb, closeDb };
  }

  function resolveAssetUrl(apiBase, rawUrl) {
    if (!rawUrl) return '';
    if (rawUrl.startsWith('/')) {
      return `${apiBase}${rawUrl}`;
    }
    return rawUrl;
  }

  function extractImagesRelativePath(resolvedUrl) {
    if (typeof resolvedUrl !== 'string') return '';
    const noQuery = resolvedUrl.split('?')[0];
    if (noQuery.startsWith('images/')) {
      try {
        return decodeURIComponent(noQuery);
      } catch (e) {
        return noQuery;
      }
    }
    return '';
  }

  const apiBase = resolveApiBase();

  // DOM 要素
  const connBadge = document.getElementById('conn-badge');
  const debugOpenLink = document.getElementById('debug-open-link');
  const statusBanner = document.getElementById('status-banner');
  const tokenPanel = document.getElementById('token-panel');
  const apiTokenInput = document.getElementById('api-token-input');
  const btnSaveToken = document.getElementById('btn-save-token');

  const avatarSelect = document.getElementById('avatar-select');
  const newAvatarName = document.getElementById('new-avatar-name');
  const btnAddAvatar = document.getElementById('btn-add-avatar');

  const btnRefreshInputs = document.getElementById('btn-refresh-inputs');
  const micSelectWrap = document.getElementById('mic-select-wrap');
  const micFilterInput = document.getElementById('mic-filter');
  const btnClearMicFilter = document.getElementById('btn-clear-mic-filter');
  const micSelect = document.getElementById('mic-select');
  const micTextWrap = document.getElementById('mic-text-wrap');
  const micText = document.getElementById('mic-text');
  const micStatusNote = document.getElementById('mic-status-note');

  const liveLevelText = document.getElementById('live-level-text');
  const liveMouthPill = document.getElementById('live-mouth-pill');
  const meterFill = document.getElementById('meter-fill');
  const markerClose = document.getElementById('marker-close');
  const markerOpen = document.getElementById('marker-open');

  const openRange = document.getElementById('open-threshold-range');
  const openNum = document.getElementById('open-threshold-num');
  const closeRange = document.getElementById('close-threshold-range');
  const closeNum = document.getElementById('close-threshold-num');
  const crosstalkRange = document.getElementById('crosstalk-margin-range');
  const crosstalkNum = document.getElementById('crosstalk-margin-num');

  const dirtyIndicator = document.getElementById('dirty-indicator');
  const btnReload = document.getElementById('btn-reload');
  const btnSave = document.getElementById('btn-save');
  const overlayUrlText = document.getElementById('overlay-url-text');
  const btnCopyUrl = document.getElementById('btn-copy-url');

  const slotCards = {};
  for (const slot of SLOTS) {
    const card = document.querySelector(`.slot-card[data-slot="${slot}"]`);
    if (!card) continue;
    slotCards[slot] = {
      card,
      previewImg: card.querySelector('.slot-preview'),
      previewEmpty: card.querySelector('.preview-empty'),
      labelEl: card.querySelector('.slot-current-label'),
      selectEl: card.querySelector('.slot-select'),
      uploadBtn: card.querySelector('.upload-btn'),
      uploadSpan: card.querySelector('.upload-btn span'),
      fileInput: card.querySelector('.slot-file-input'),
    };
  }

  const state = {
    avatar: '',
    avatarNames: [],
    config: null,
    availableImages: [],
    obsInputsConnected: false,
    obsInputs: [],
    obsInputsError: '',
    micValue: '',
    micFilter: '',
    openThresholdDb: linearToClampedDb(DEFAULT_OPEN_LINEAR, DEFAULT_OPEN_LINEAR),
    closeThresholdDb: linearToClampedDb(DEFAULT_CLOSE_LINEAR, DEFAULT_CLOSE_LINEAR),
    crosstalkMarginDb: 0.0,
    pendingImageSelections: {},
    dirty: false,
    liveLevel: 0,
    liveMouthOpen: false,
    crosstalkSuppressed: false,
    deviceConnected: false,
    serverReachable: false,
    transport: typeof EventSource === 'function' ? 'sse' : 'poll',
    pollMs: 120,
  };

  let sseSource = null;
  let pollTimer = null;
  let bannerTimer = null;
  let avatarSwitchGen = 0;

  function avatarQueryString() {
    return state.avatar ? `avatar=${encodeURIComponent(state.avatar)}` : '';
  }

  function withAvatarQuery(path) {
    const qs = avatarQueryString();
    return qs ? `${path}?${qs}` : path;
  }

  function configUrl() {
    return withAvatarQuery(`${apiBase}/api/pngtuber/config`);
  }

  function renderAvatarSelect() {
    if (!avatarSelect) return;
    avatarSelect.innerHTML = '';
    const mainOpt = document.createElement('option');
    mainOpt.value = '';
    mainOpt.textContent = 'メインアバター';
    avatarSelect.appendChild(mainOpt);
    for (const name of state.avatarNames) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      avatarSelect.appendChild(opt);
    }
    avatarSelect.value = state.avatar;
  }

  function updateOverlayUrl() {
    if (debugOpenLink) {
      debugOpenLink.href = state.avatar
        ? `index.html?debug=1&avatar=${encodeURIComponent(state.avatar)}`
        : 'index.html?debug=1';
    }
    if (!overlayUrlText) return;
    let base;
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      const basePath = location.pathname.replace(/\/[^/]*$/, '/');
      base = `${location.origin}${basePath}index.html`;
    } else if (apiBase) {
      base = `${apiBase}/PngTuber/index.html`;
    } else {
      base = (overlayUrlText.textContent || '').split('?')[0];
    }
    overlayUrlText.textContent = state.avatar
      ? `${base}?avatar=${encodeURIComponent(state.avatar)}`
      : base;
  }

  function showBanner(message, kind = 'info', autoHideMs = 0) {
    if (!statusBanner) return;
    if (bannerTimer !== null) {
      clearTimeout(bannerTimer);
      bannerTimer = null;
    }
    statusBanner.textContent = message;
    statusBanner.className = `status-banner is-${kind}`;
    statusBanner.hidden = false;
    if (autoHideMs > 0) {
      bannerTimer = setTimeout(() => {
        bannerTimer = null;
        statusBanner.hidden = true;
      }, autoHideMs);
    }
  }

  function setDirty(flag) {
    state.dirty = Boolean(flag);
    if (dirtyIndicator) {
      dirtyIndicator.hidden = !state.dirty;
    }
  }

  function updateConnectionBadge() {
    if (!connBadge) return;
    if (!state.serverReachable) {
      connBadge.textContent = 'サーバー未接続';
      connBadge.className = 'badge badge-error';
      return;
    }
    const matched = state.config && state.config.matched_input_name;
    if (state.deviceConnected) {
      const label = matched ? `OBS 接続中: ${matched}` : 'OBS 接続中';
      connBadge.textContent = `${label} [${state.transport}]`;
      connBadge.className = 'badge badge-ok';
    } else {
      connBadge.textContent = `OBS 未接続 [${state.transport}]`;
      connBadge.className = 'badge badge-warn';
    }
  }

  function updateThresholdUI() {
    const openStr = state.openThresholdDb.toFixed(1);
    const closeStr = state.closeThresholdDb.toFixed(1);

    if (openRange) openRange.value = openStr;
    if (openNum && document.activeElement !== openNum) openNum.value = openStr;
    if (closeRange) closeRange.value = closeStr;
    if (closeNum && document.activeElement !== closeNum) closeNum.value = closeStr;

    const crosstalkStr = state.crosstalkMarginDb.toFixed(1);
    if (crosstalkRange) crosstalkRange.value = crosstalkStr;
    if (crosstalkNum && document.activeElement !== crosstalkNum) crosstalkNum.value = crosstalkStr;

    updateMeterUI();
  }

  function updateMeterUI() {
    const levelDb = linearToDb(state.liveLevel);
    const fillPct = dbToMeterPercent(levelDb);
    const openPct = dbToMeterPercent(state.openThresholdDb);
    const closePct = dbToMeterPercent(state.closeThresholdDb);

    if (meterFill) {
      meterFill.style.width = `${fillPct.toFixed(1)}%`;
      meterFill.classList.toggle('is-speaking', state.liveMouthOpen);
    }
    if (markerOpen) {
      markerOpen.style.left = `${openPct.toFixed(1)}%`;
      markerOpen.title = `open_threshold: ${state.openThresholdDb.toFixed(1)} dB（口を開く）`;
    }
    if (markerClose) {
      markerClose.style.left = `${closePct.toFixed(1)}%`;
      markerClose.title = `close_threshold: ${state.closeThresholdDb.toFixed(1)} dB（口を閉じる）`;
    }
    if (liveLevelText) {
      liveLevelText.textContent = `level: ${formatLevelDb(state.liveLevel)}`;
    }
    if (liveMouthPill) {
      if (!state.deviceConnected) {
        liveMouthPill.textContent = '口: 閉 (未接続)';
        liveMouthPill.className = 'badge badge-muted';
      } else if (state.crosstalkSuppressed) {
        liveMouthPill.textContent = '口: 閉 (クロストーク抑制)';
        liveMouthPill.className = 'badge badge-warn';
      } else if (state.liveMouthOpen) {
        liveMouthPill.textContent = '口: 開';
        liveMouthPill.className = 'badge badge-ok';
      } else {
        liveMouthPill.textContent = '口: 閉';
        liveMouthPill.className = 'badge badge-muted';
      }
    }
  }

  function applyLiveSample(sample) {
    state.serverReachable = true;
    const prevConnected = state.deviceConnected;
    state.deviceConnected = Boolean(sample && sample.device_connected);
    state.crosstalkSuppressed = state.deviceConnected && Boolean(sample && sample.crosstalk_suppressed);
    const level = state.deviceConnected ? clamp(sample && sample.level, 0, 1, 0) : 0;
    state.liveLevel = level;

    const openLinear = dbToLinear(state.openThresholdDb);
    const closeLinear = Math.min(openLinear, dbToLinear(state.closeThresholdDb));

    if (!state.deviceConnected || state.crosstalkSuppressed) {
      state.liveMouthOpen = false;
    } else if (state.liveMouthOpen) {
      if (level <= closeLinear) {
        state.liveMouthOpen = false;
      }
    } else if (level >= openLinear) {
      state.liveMouthOpen = true;
    }

    if (!prevConnected && state.deviceConnected && (!state.config || !state.config.matched_input_name)) {
      refreshMatchedConfigLabel();
    }
    updateConnectionBadge();
    updateMeterUI();
  }

  async function refreshMatchedConfigLabel() {
    const gen = avatarSwitchGen;
    const expectedAvatar = state.avatar;
    try {
      const res = await apiFetch(configUrl(), { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      if (gen !== avatarSwitchGen || expectedAvatar !== state.avatar) return;
      if (state.config && data && typeof data.matched_input_name === 'string') {
        state.config.matched_input_name = data.matched_input_name;
        updateConnectionBadge();
        renderMicControl();
      }
    } catch (e) {}
  }

  function renderMicControl() {
    const current = state.micValue;
    const matched = state.config && state.config.matched_input_name;
    const rawFilter = state.micFilter || '';
    const filterText = rawFilter.trim().toLowerCase();

    if (btnClearMicFilter) {
      btnClearMicFilter.hidden = !rawFilter;
    }

    if (state.obsInputsConnected) {
      if (micSelectWrap) micSelectWrap.hidden = false;
      if (micTextWrap) micTextWrap.hidden = true;

      // サーバーが返す「マイク系が先頭」の並び順を保ったまま名前で絞り込む
      const filteredInputs = filterText
        ? state.obsInputs.filter((name) => name.toLowerCase().includes(filterText))
        : state.obsInputs;

      if (micSelect) {
        micSelect.innerHTML = '';

        const autoOpt = document.createElement('option');
        autoOpt.value = '';
        autoOpt.textContent = '自動選択（OBS のマイク入力を自動検出）';
        micSelect.appendChild(autoOpt);

        let foundCurrent = current === '';
        for (const name of filteredInputs) {
          const opt = document.createElement('option');
          opt.value = name;
          opt.textContent = name;
          if (name === current) foundCurrent = true;
          micSelect.appendChild(opt);
        }

        if (filterText && filteredInputs.length === 0) {
          const emptyOpt = document.createElement('option');
          emptyOpt.value = '__no_match__';
          emptyOpt.disabled = true;
          emptyOpt.textContent = '（一致する音声入力がありません）';
          micSelect.appendChild(emptyOpt);
        }

        if (current && !foundCurrent) {
          const customOpt = document.createElement('option');
          customOpt.value = current;
          const hiddenByFilter = filterText && state.obsInputs.includes(current);
          customOpt.textContent = hiddenByFilter
            ? `[選択中（絞り込み外）] ${current}`
            : `[現在の設定] ${current}`;
          micSelect.appendChild(customOpt);
        }

        micSelect.value = current;
      }

      if (micStatusNote) {
        const countLabel = filterText
          ? `OBS 音声入力: ${filteredInputs.length} / ${state.obsInputs.length} 件表示`
          : `OBS 音声入力: ${state.obsInputs.length} 件取得済み`;
        const parts = [countLabel];
        if (matched) {
          parts.push(`現在の検出ソース: ${matched}`);
        }
        micStatusNote.textContent = parts.join(' / ');
      }
    } else {
      if (micSelectWrap) micSelectWrap.hidden = true;
      if (micTextWrap) micTextWrap.hidden = false;
      if (micText && micText.value !== current) {
        micText.value = current;
      }
      if (micStatusNote) {
        micStatusNote.textContent = 'OBS に接続できなかったため、ソース名の直接入力にフォールバックしています（OBS 起動後に「一覧を再取得」で選択式に切替できます）。';
      }
    }
  }

  function setSlotPreview(slot, url, labelText) {
    const ui = slotCards[slot];
    if (!ui) return;
    if (ui.labelEl) {
      ui.labelEl.textContent = labelText ? `現在: ${labelText}` : '現在: 未設定';
    }
    const resolved = resolveAssetUrl(apiBase, url);
    if (!resolved) {
      if (ui.previewImg) {
        ui.previewImg.hidden = true;
        ui.previewImg.removeAttribute('src');
      }
      if (ui.previewEmpty) {
        ui.previewEmpty.hidden = false;
        ui.previewEmpty.textContent = '画像未設定';
      }
      return;
    }
    if (ui.previewImg) {
      ui.previewImg.onload = () => {
        ui.previewImg.hidden = false;
        if (ui.previewEmpty) ui.previewEmpty.hidden = true;
      };
      ui.previewImg.onerror = () => {
        ui.previewImg.hidden = true;
        if (ui.previewEmpty) {
          ui.previewEmpty.hidden = false;
          ui.previewEmpty.textContent = '画像を読み込めません';
        }
      };
      ui.previewImg.src = resolved;
    }
  }

  function renderSlotControls() {
    const cfgImages = (state.config && state.config.images) || {};
    const cfgLabels = (state.config && state.config.image_labels) || {};

    for (const slot of SLOTS) {
      const ui = slotCards[slot];
      if (!ui || !ui.selectEl) continue;

      const pendingPath = state.pendingImageSelections[slot] || '';
      const serverUrl = cfgImages[slot] || '';
      const serverRel = extractImagesRelativePath(serverUrl);
      const activeRel = pendingPath || serverRel;
      const serverLabel = cfgLabels[slot] || serverRel || '';

      ui.selectEl.innerHTML = '';

      const isExternalOrUnlisted = !pendingPath && (!serverRel || !state.availableImages.includes(serverRel));
      if (isExternalOrUnlisted) {
        const keepOpt = document.createElement('option');
        keepOpt.value = '';
        keepOpt.textContent = serverLabel
          ? `現在の設定を維持 (${serverLabel})`
          : 'images/ から既存の画像を選択...';
        ui.selectEl.appendChild(keepOpt);
      }

      if (state.availableImages.length === 0) {
        const emptyOpt = document.createElement('option');
        emptyOpt.value = '';
        emptyOpt.disabled = true;
        emptyOpt.textContent = '(PngTuber/images/ に既存ファイルがありません)';
        ui.selectEl.appendChild(emptyOpt);
      } else {
        for (const relPath of state.availableImages) {
          const opt = document.createElement('option');
          opt.value = relPath;
          opt.textContent = relPath;
          ui.selectEl.appendChild(opt);
        }
      }

      if (activeRel && state.availableImages.includes(activeRel)) {
        ui.selectEl.value = activeRel;
      } else {
        ui.selectEl.value = '';
      }

      if (pendingPath) {
        const encodedRel = pendingPath.split('/').map(encodeURIComponent).join('/');
        setSlotPreview(slot, `${encodedRel}?t=${Date.now()}`, `${pendingPath}（未保存）`);
      } else {
        setSlotPreview(slot, serverUrl, serverLabel);
      }
    }
  }

  function recomputeDirty() {
    if (!state.config) {
      setDirty(false);
      return;
    }
    const cfgMic = typeof state.config.obs_input_name === 'string' ? state.config.obs_input_name : '';
    const { openDb: cfgOpenDb, closeDb: cfgCloseDb } = resolveConfigThresholdsDb(state.config);
    const cfgCrosstalk = round1(clamp(state.config.crosstalk_margin_db, 0.0, 24.0, 0.0));
    const hasPendingImages = Object.keys(state.pendingImageSelections).length > 0;
    const isChanged = hasPendingImages
      || state.micValue !== cfgMic
      || state.openThresholdDb !== cfgOpenDb
      || state.closeThresholdDb !== cfgCloseDb
      || state.crosstalkMarginDb !== cfgCrosstalk;
    setDirty(isChanged);
  }

  function applyServerConfig(cfg, preserveLocalEdits = false) {
    state.config = cfg || {};
    if (Array.isArray(state.config.avatars)) {
      state.avatarNames = state.config.avatars.slice();
    }
    renderAvatarSelect();
    updateOverlayUrl();
    state.pollMs = clamp(state.config.poll_ms, 50, 2000, 120);

    if (!preserveLocalEdits) {
      const { openDb, closeDb } = resolveConfigThresholdsDb(state.config);
      state.micValue = typeof state.config.obs_input_name === 'string' ? state.config.obs_input_name : '';
      state.openThresholdDb = openDb;
      state.closeThresholdDb = closeDb;
      state.crosstalkMarginDb = round1(clamp(state.config.crosstalk_margin_db, 0.0, 24.0, 0.0));
      state.pendingImageSelections = {};
      setDirty(false);
    }

    updateThresholdUI();
    renderMicControl();
    renderSlotControls();
    updateConnectionBadge();
  }

  async function fetchConfig(preserveLocalEdits = false, expectedGen = avatarSwitchGen) {
    const expectedAvatar = state.avatar;
    const res = await apiFetch(configUrl(), { cache: 'no-store' });
    if (!res.ok) {
      throw new Error(`設定の読み込みに失敗しました (HTTP ${res.status})`);
    }
    const data = await res.json();
    if (expectedGen !== avatarSwitchGen || expectedAvatar !== state.avatar) {
      return false;
    }
    state.serverReachable = true;
    applyServerConfig(data, preserveLocalEdits);
    return true;
  }

  async function fetchAvailableImages() {
    try {
      const res = await fetch(`${apiBase}/api/pngtuber/images`, { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      state.availableImages = Array.isArray(data && data.images) ? data.images : [];
      renderSlotControls();
    } catch (e) {}
  }

  async function fetchObsInputs(refresh = false) {
    if (btnRefreshInputs) btnRefreshInputs.disabled = true;
    try {
      const url = `${apiBase}/api/pngtuber/inputs${refresh ? '?refresh=1' : ''}`;
      const res = await apiFetch(url, { cache: 'no-store' });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      state.obsInputsConnected = Boolean(data && data.connected);
      state.obsInputs = Array.isArray(data && data.inputs) ? data.inputs : [];
      state.obsInputsError = (data && data.error) || '';
    } catch (e) {
      state.obsInputsConnected = false;
      state.obsInputs = [];
      state.obsInputsError = e.message || '';
    } finally {
      if (btnRefreshInputs) btnRefreshInputs.disabled = false;
      renderMicControl();
    }
  }

  function handleUnauthorized() {
    if (tokenPanel) tokenPanel.hidden = false;
    showBanner('認証が必要です。上部の API トークン欄にトークンを入力してから再試行してください。', 'error');
  }

  async function saveSettings() {
    if (btnSave) btnSave.disabled = true;
    const gen = avatarSwitchGen;
    const targetAvatar = state.avatar;
    const openLinear = dbToLinear(state.openThresholdDb);
    const closeLinear = Math.min(openLinear, dbToLinear(state.closeThresholdDb));
    const payload = {
      obs_input_name: state.micValue,
      open_threshold: openLinear,
      close_threshold: closeLinear,
      crosstalk_margin_db: state.crosstalkMarginDb,
    };
    if (targetAvatar) {
      payload.avatar = targetAvatar;
    }
    if (Object.keys(state.pendingImageSelections).length > 0) {
      payload.images = Object.assign({}, state.pendingImageSelections);
    }

    try {
      const res = await apiFetch(`${apiBase}/api/pngtuber/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.status === 401) {
        handleUnauthorized();
        return;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || `保存に失敗しました (HTTP ${res.status})`);
      }
      if (tokenPanel) tokenPanel.hidden = true;
      if (gen === avatarSwitchGen && targetAvatar === state.avatar) {
        if (data.config) {
          applyServerConfig(data.config, false);
        } else {
          await fetchConfig(false, gen);
        }
      }
      showBanner('設定を pngtuber_config.json に保存しました（サーバーへ自動反映されます）。', 'ok', 4500);
    } catch (e) {
      showBanner(`保存エラー: ${e.message}`, 'error');
    } finally {
      if (btnSave) btnSave.disabled = false;
    }
  }

  async function uploadSlotImage(slot, file) {
    const ui = slotCards[slot];
    if (!file) return;
    if (!/\.png$/i.test(file.name || '')) {
      showBanner('アップロードできる画像は .png ファイルのみです。', 'error');
      return;
    }

    const gen = avatarSwitchGen;
    const targetAvatar = state.avatar;
    if (ui && ui.uploadBtn) ui.uploadBtn.classList.add('is-uploading');
    if (ui && ui.uploadSpan) ui.uploadSpan.textContent = 'アップロード中...';

    try {
      const formData = new FormData();
      formData.append('file', file, file.name);
      const uploadUrl = withAvatarQuery(`${apiBase}/api/pngtuber/image/${encodeURIComponent(slot)}`);
      const res = await apiFetch(uploadUrl, {
        method: 'POST',
        body: formData,
      });
      if (res.status === 401) {
        handleUnauthorized();
        return;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || `アップロードに失敗しました (HTTP ${res.status})`);
      }
      if (tokenPanel) tokenPanel.hidden = true;
      if (gen === avatarSwitchGen && targetAvatar === state.avatar) {
        delete state.pendingImageSelections[slot];
        if (data.config) {
          state.config = data.config;
        }
      }
      await fetchAvailableImages();
      if (gen === avatarSwitchGen && targetAvatar === state.avatar) {
        renderSlotControls();
        recomputeDirty();
      }
      showBanner(`${slot} の画像を ${data.path} に更新しました。`, 'ok', 4000);
    } catch (e) {
      showBanner(`アップロードエラー: ${e.message}`, 'error');
    } finally {
      if (ui && ui.uploadBtn) ui.uploadBtn.classList.remove('is-uploading');
      if (ui && ui.uploadSpan) ui.uploadSpan.textContent = 'PNG をアップロード';
      if (ui && ui.fileInput) ui.fileInput.value = '';
    }
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function closeSse() {
    if (sseSource !== null) {
      try {
        sseSource.close();
      } catch (e) {}
      sseSource = null;
    }
  }

  async function pollLevelOnce(gen = avatarSwitchGen) {
    const expectedAvatar = state.avatar;
    if (gen !== avatarSwitchGen) return;
    try {
      const res = await fetch(withAvatarQuery(`${apiBase}/api/pngtuber/level`), { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (gen !== avatarSwitchGen || expectedAvatar !== state.avatar) return;
      applyLiveSample(data);
    } catch (e) {
      if (gen !== avatarSwitchGen || expectedAvatar !== state.avatar) return;
      state.serverReachable = false;
      state.deviceConnected = false;
      state.liveLevel = 0;
      state.liveMouthOpen = false;
      state.crosstalkSuppressed = false;
      updateConnectionBadge();
      updateMeterUI();
    } finally {
      if (gen === avatarSwitchGen && expectedAvatar === state.avatar && state.transport === 'poll') {
        stopPolling();
        pollTimer = setTimeout(() => pollLevelOnce(gen), state.pollMs);
      }
    }
  }

  function startLiveStream(gen = avatarSwitchGen) {
    if (gen !== avatarSwitchGen) return;
    closeSse();
    stopPolling();
    if (typeof EventSource !== 'function') {
      state.transport = 'poll';
      pollLevelOnce(gen);
      return;
    }
    state.transport = 'sse';
    const es = new EventSource(withAvatarQuery(`${apiBase}/api/pngtuber/stream`));
    sseSource = es;

    es.onopen = () => {
      if (sseSource !== es || gen !== avatarSwitchGen) return;
      state.serverReachable = true;
      state.transport = 'sse';
      updateConnectionBadge();
    };

    es.onmessage = (event) => {
      if (sseSource !== es || gen !== avatarSwitchGen) return;
      try {
        const data = JSON.parse(event.data);
        applyLiveSample(data);
      } catch (e) {}
    };

    es.onerror = () => {
      if (sseSource !== es || gen !== avatarSwitchGen) return;
      state.deviceConnected = false;
      state.liveLevel = 0;
      state.liveMouthOpen = false;
      updateConnectionBadge();
      updateMeterUI();
      const closedState = typeof EventSource.CLOSED === 'number' ? EventSource.CLOSED : 2;
      if (es.readyState === closedState) {
        closeSse();
        state.transport = 'poll';
        pollLevelOnce(gen);
      }
    };
  }

  async function switchAvatar(name) {
    const next = (name || '').trim();
    if (next === state.avatar) return;
    const gen = ++avatarSwitchGen;
    closeSse();
    stopPolling();
    state.avatar = next;
    state.pendingImageSelections = {};
    state.liveLevel = 0;
    state.liveMouthOpen = false;
    state.crosstalkSuppressed = false;
    state.deviceConnected = false;
    setDirty(false);
    renderAvatarSelect();
    updateOverlayUrl();
    updateMeterUI();
    try {
      await fetchConfig(false, gen);
    } catch (e) {
      if (gen !== avatarSwitchGen || state.avatar !== next) return;
      showBanner(e.message || 'アバター設定の読み込みに失敗しました。', 'error');
    }
    if (gen !== avatarSwitchGen || state.avatar !== next) return;
    startLiveStream(gen);
  }

  async function addAvatar() {
    const name = (newAvatarName && newAvatarName.value || '').trim();
    if (!name) {
      showBanner('新規アバター名を入力してください。', 'error');
      return;
    }
    if (state.avatarNames.includes(name)) {
      showBanner(`アバター「${name}」は既に存在します。`, 'error');
      return;
    }
    if (btnAddAvatar) btnAddAvatar.disabled = true;
    try {
      const res = await apiFetch(`${apiBase}/api/pngtuber/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ avatar: name }),
      });
      if (res.status === 401) {
        handleUnauthorized();
        return;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || `アバターの追加に失敗しました (HTTP ${res.status})`);
      }
      if (tokenPanel) tokenPanel.hidden = true;
      if (newAvatarName) newAvatarName.value = '';
      if (!state.avatarNames.includes(name)) {
        state.avatarNames.push(name);
        state.avatarNames.sort();
      }
      showBanner(`アバター「${name}」を追加しました。`, 'ok', 3000);
      await switchAvatar(name);
    } catch (e) {
      showBanner(`追加エラー: ${e.message}`, 'error');
    } finally {
      if (btnAddAvatar) btnAddAvatar.disabled = false;
    }
  }

  function bindEvents() {
    if (avatarSelect) {
      avatarSelect.addEventListener('change', () => {
        switchAvatar(avatarSelect.value);
      });
    }
    if (btnAddAvatar) {
      btnAddAvatar.addEventListener('click', () => addAvatar());
    }
    if (micFilterInput) {
      micFilterInput.addEventListener('input', () => {
        state.micFilter = micFilterInput.value;
        renderMicControl();
      });
    }
    if (btnClearMicFilter) {
      btnClearMicFilter.addEventListener('click', () => {
        state.micFilter = '';
        if (micFilterInput) {
          micFilterInput.value = '';
          micFilterInput.focus();
        }
        renderMicControl();
      });
    }
    if (micSelect) {
      micSelect.addEventListener('change', () => {
        state.micValue = micSelect.value;
        recomputeDirty();
        renderMicControl();
      });
    }
    if (micText) {
      micText.addEventListener('input', () => {
        state.micValue = micText.value.trim();
        recomputeDirty();
      });
    }
    if (btnRefreshInputs) {
      btnRefreshInputs.addEventListener('click', () => {
        fetchObsInputs(true);
      });
    }

    const onOpenChange = (rawVal) => {
      const nextOpenDb = round1(clamp(rawVal, DB_MIN, DB_MAX, state.openThresholdDb));
      state.openThresholdDb = nextOpenDb;
      if (state.closeThresholdDb > nextOpenDb) {
        state.closeThresholdDb = nextOpenDb;
      }
      recomputeDirty();
      updateThresholdUI();
    };

    const onCloseChange = (rawVal) => {
      const nextCloseDb = round1(clamp(rawVal, DB_MIN, state.openThresholdDb, state.closeThresholdDb));
      state.closeThresholdDb = nextCloseDb;
      recomputeDirty();
      updateThresholdUI();
    };

    const onCrosstalkChange = (rawVal) => {
      const nextMargin = round1(clamp(rawVal, 0.0, 24.0, state.crosstalkMarginDb));
      state.crosstalkMarginDb = nextMargin;
      recomputeDirty();
      updateThresholdUI();
    };

    if (openRange) {
      openRange.addEventListener('input', () => onOpenChange(openRange.value));
    }
    if (openNum) {
      openNum.addEventListener('input', () => onOpenChange(openNum.value));
      openNum.addEventListener('blur', () => updateThresholdUI());
    }
    if (closeRange) {
      closeRange.addEventListener('input', () => onCloseChange(closeRange.value));
    }
    if (closeNum) {
      closeNum.addEventListener('input', () => onCloseChange(closeNum.value));
      closeNum.addEventListener('blur', () => updateThresholdUI());
    }
    if (crosstalkRange) {
      crosstalkRange.addEventListener('input', () => onCrosstalkChange(crosstalkRange.value));
    }
    if (crosstalkNum) {
      crosstalkNum.addEventListener('input', () => onCrosstalkChange(crosstalkNum.value));
      crosstalkNum.addEventListener('blur', () => updateThresholdUI());
    }

    for (const slot of SLOTS) {
      const ui = slotCards[slot];
      if (!ui) continue;
      if (ui.selectEl) {
        ui.selectEl.addEventListener('change', () => {
          const val = ui.selectEl.value;
          const serverUrl = (state.config && state.config.images && state.config.images[slot]) || '';
          const serverRel = extractImagesRelativePath(serverUrl);
          if (!val || val === serverRel) {
            delete state.pendingImageSelections[slot];
          } else {
            state.pendingImageSelections[slot] = val;
          }
          recomputeDirty();
          renderSlotControls();
        });
      }
      if (ui.fileInput) {
        ui.fileInput.addEventListener('change', () => {
          const file = ui.fileInput.files && ui.fileInput.files[0];
          if (file) {
            uploadSlotImage(slot, file);
          }
        });
      }
    }

    if (btnSave) {
      btnSave.addEventListener('click', () => saveSettings());
    }
    if (btnReload) {
      btnReload.addEventListener('click', async () => {
        const gen = avatarSwitchGen;
        try {
          await Promise.all([
            fetchConfig(false, gen),
            fetchAvailableImages(),
            fetchObsInputs(true),
          ]);
          if (gen === avatarSwitchGen) {
            showBanner('サーバーから最新の設定を再読み込みしました。', 'info', 3000);
          }
        } catch (e) {
          if (gen === avatarSwitchGen) {
            showBanner(e.message || '再読み込みに失敗しました。', 'error');
          }
        }
      });
    }
    if (btnSaveToken) {
      btnSaveToken.addEventListener('click', async () => {
        const token = (apiTokenInput && apiTokenInput.value || '').trim();
        if (typeof window.setWidgetApiToken === 'function') {
          window.setWidgetApiToken(token);
        }
        if (tokenPanel) tokenPanel.hidden = true;
        const gen = avatarSwitchGen;
        try {
          await Promise.all([
            fetchConfig(false, gen),
            fetchObsInputs(true),
          ]);
          if (gen === avatarSwitchGen) {
            showBanner('API トークンを適用し、設定と入力一覧を再取得しました。', 'info', 4000);
          }
        } catch (e) {
          if (gen === avatarSwitchGen) {
            showBanner(e.message || 'API トークン適用後の再取得に失敗しました。', 'error');
          }
        }
      });
    }
    if (btnCopyUrl && overlayUrlText) {
      btnCopyUrl.addEventListener('click', async () => {
        const text = overlayUrlText.textContent || '';
        try {
          await navigator.clipboard.writeText(text);
          showBanner('OBS ブラウザソース用 URL をコピーしました。', 'ok', 2500);
        } catch (e) {
          showBanner('URL をコピーできませんでした。手動で選択してコピーしてください。', 'error', 3500);
        }
      });
    }

    window.addEventListener('beforeunload', () => {
      avatarSwitchGen += 1;
      closeSse();
      stopPolling();
    });
  }

  function initOverlayUrlHint() {
    updateOverlayUrl();
  }

  async function init() {
    initOverlayUrlHint();
    bindEvents();
    updateThresholdUI();
    updateConnectionBadge();

    const gen = avatarSwitchGen;
    try {
      await Promise.all([
        fetchConfig(false, gen),
        fetchAvailableImages(),
        fetchObsInputs(false),
      ]);
    } catch (e) {
      if (gen === avatarSwitchGen) {
        showBanner(e.message || 'サーバーから設定を読み込めませんでした。統合サーバーが起動しているか確認してください。', 'error');
      }
    }

    if (gen === avatarSwitchGen) {
      startLiveStream(gen);
    }
  }

  init();
})();
