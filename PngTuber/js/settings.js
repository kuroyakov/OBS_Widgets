/**
 * PNGTuber 設定ページ制御スクリプト (PngTuber/js/settings.js)
 * - メインアバター設定の読み込み・マージ保存 (GET/POST /api/pngtuber/config)
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
    const n = Number(num);
    if (!Number.isFinite(n)) return fallback;
    return Math.max(min, Math.min(max, n));
  }

  function round3(num) {
    return Math.round(num * 1000) / 1000;
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
  const statusBanner = document.getElementById('status-banner');
  const tokenPanel = document.getElementById('token-panel');
  const apiTokenInput = document.getElementById('api-token-input');
  const btnSaveToken = document.getElementById('btn-save-token');

  const btnRefreshInputs = document.getElementById('btn-refresh-inputs');
  const micSelectWrap = document.getElementById('mic-select-wrap');
  const micSelect = document.getElementById('mic-select');
  const micTextWrap = document.getElementById('mic-text-wrap');
  const micText = document.getElementById('mic-text');
  const micStatusNote = document.getElementById('mic-status-note');

  const liveLevelText = document.getElementById('live-level-text');
  const liveMouthPill = document.getElementById('live-mouth-pill');
  const meterFill = document.getElementById('meter-fill');
  const markerClose = document.getElementById('marker-close');
  const markerOpen = document.getElementById('marker-open');
  const meterScaleMax = document.getElementById('meter-scale-max');

  const openRange = document.getElementById('open-threshold-range');
  const openNum = document.getElementById('open-threshold-num');
  const closeRange = document.getElementById('close-threshold-range');
  const closeNum = document.getElementById('close-threshold-num');

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
    config: null,
    availableImages: [],
    obsInputsConnected: false,
    obsInputs: [],
    obsInputsError: '',
    micValue: '',
    openThreshold: 0.05,
    closeThreshold: 0.025,
    pendingImageSelections: {},
    dirty: false,
    liveLevel: 0,
    liveMouthOpen: false,
    deviceConnected: false,
    serverReachable: false,
    transport: typeof EventSource === 'function' ? 'sse' : 'poll',
    pollMs: 120,
  };

  let sseSource = null;
  let pollTimer = null;
  let bannerTimer = null;

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

  function computeMeterScaleMax() {
    return Math.min(1.0, Math.max(0.25, state.openThreshold * 2.5));
  }

  function updateThresholdUI() {
    const openStr = state.openThreshold.toFixed(3);
    const closeStr = state.closeThreshold.toFixed(3);

    if (openRange) openRange.value = openStr;
    if (openNum && document.activeElement !== openNum) openNum.value = openStr;
    if (closeRange) closeRange.value = closeStr;
    if (closeNum && document.activeElement !== closeNum) closeNum.value = closeStr;

    updateMeterUI();
  }

  function updateMeterUI() {
    const scaleMax = computeMeterScaleMax();
    const fillPct = Math.min(100, Math.max(0, (state.liveLevel / scaleMax) * 100));
    const openPct = Math.min(100, Math.max(0, (state.openThreshold / scaleMax) * 100));
    const closePct = Math.min(100, Math.max(0, (state.closeThreshold / scaleMax) * 100));

    if (meterScaleMax) {
      meterScaleMax.textContent = scaleMax.toFixed(3);
    }
    if (meterFill) {
      meterFill.style.width = `${fillPct.toFixed(1)}%`;
      meterFill.classList.toggle('is-speaking', state.liveMouthOpen);
    }
    if (markerOpen) {
      markerOpen.style.left = `${openPct.toFixed(1)}%`;
    }
    if (markerClose) {
      markerClose.style.left = `${closePct.toFixed(1)}%`;
    }
    if (liveLevelText) {
      liveLevelText.textContent = `level: ${state.liveLevel.toFixed(4)}`;
    }
    if (liveMouthPill) {
      if (!state.deviceConnected) {
        liveMouthPill.textContent = '口: 閉 (未接続)';
        liveMouthPill.className = 'badge badge-muted';
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
    const level = state.deviceConnected ? clamp(sample && sample.level, 0, 1, 0) : 0;
    state.liveLevel = level;

    if (!state.deviceConnected) {
      state.liveMouthOpen = false;
    } else if (state.liveMouthOpen) {
      if (level <= state.closeThreshold) {
        state.liveMouthOpen = false;
      }
    } else if (level >= state.openThreshold) {
      state.liveMouthOpen = true;
    }

    if (!prevConnected && state.deviceConnected && (!state.config || !state.config.matched_input_name)) {
      refreshMatchedConfigLabel();
    }
    updateConnectionBadge();
    updateMeterUI();
  }

  async function refreshMatchedConfigLabel() {
    try {
      const res = await fetch(`${apiBase}/api/pngtuber/config`, { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
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

    if (state.obsInputsConnected) {
      if (micSelectWrap) micSelectWrap.hidden = false;
      if (micTextWrap) micTextWrap.hidden = true;

      if (micSelect) {
        micSelect.innerHTML = '';

        const autoOpt = document.createElement('option');
        autoOpt.value = '';
        autoOpt.textContent = '自動選択（OBS のマイク入力を自動検出）';
        micSelect.appendChild(autoOpt);

        let foundCurrent = current === '';
        for (const name of state.obsInputs) {
          const opt = document.createElement('option');
          opt.value = name;
          opt.textContent = name;
          if (name === current) foundCurrent = true;
          micSelect.appendChild(opt);
        }

        if (current && !foundCurrent) {
          const customOpt = document.createElement('option');
          customOpt.value = current;
          customOpt.textContent = `[現在の設定] ${current}`;
          micSelect.appendChild(customOpt);
        }

        micSelect.value = current;
      }

      if (micStatusNote) {
        const parts = [`OBS 音声入力: ${state.obsInputs.length} 件取得済み`];
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
    const cfgOpen = round3(clamp(state.config.open_threshold, 0.001, 1.0, 0.05));
    const cfgClose = round3(clamp(state.config.close_threshold, 0.0, cfgOpen, Math.min(cfgOpen, 0.025)));
    const hasPendingImages = Object.keys(state.pendingImageSelections).length > 0;
    const isChanged = hasPendingImages
      || state.micValue !== cfgMic
      || state.openThreshold !== cfgOpen
      || state.closeThreshold !== cfgClose;
    setDirty(isChanged);
  }

  function applyServerConfig(cfg, preserveLocalEdits = false) {
    state.config = cfg || {};
    state.pollMs = clamp(state.config.poll_ms, 50, 2000, 120);

    if (!preserveLocalEdits) {
      state.micValue = typeof state.config.obs_input_name === 'string' ? state.config.obs_input_name : '';
      state.openThreshold = round3(clamp(state.config.open_threshold, 0.001, 1.0, 0.05));
      state.closeThreshold = round3(clamp(state.config.close_threshold, 0.0, state.openThreshold, Math.min(state.openThreshold, 0.025)));
      state.pendingImageSelections = {};
      setDirty(false);
    }

    updateThresholdUI();
    renderMicControl();
    renderSlotControls();
    updateConnectionBadge();
  }

  async function fetchConfig(preserveLocalEdits = false) {
    const res = await fetch(`${apiBase}/api/pngtuber/config`, { cache: 'no-store' });
    if (!res.ok) {
      throw new Error(`設定の読み込みに失敗しました (HTTP ${res.status})`);
    }
    const data = await res.json();
    state.serverReachable = true;
    applyServerConfig(data, preserveLocalEdits);
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
      const res = await fetch(url, { cache: 'no-store' });
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
    const payload = {
      obs_input_name: state.micValue,
      open_threshold: state.openThreshold,
      close_threshold: state.closeThreshold,
    };
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
      if (data.config) {
        applyServerConfig(data.config, false);
      } else {
        await fetchConfig(false);
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

    if (ui && ui.uploadBtn) ui.uploadBtn.classList.add('is-uploading');
    if (ui && ui.uploadSpan) ui.uploadSpan.textContent = 'アップロード中...';

    try {
      const formData = new FormData();
      formData.append('file', file, file.name);
      const res = await apiFetch(`${apiBase}/api/pngtuber/image/${encodeURIComponent(slot)}`, {
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
      delete state.pendingImageSelections[slot];
      if (data.config) {
        state.config = data.config;
      }
      await fetchAvailableImages();
      renderSlotControls();
      recomputeDirty();
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

  async function pollLevelOnce() {
    try {
      const res = await fetch(`${apiBase}/api/pngtuber/level`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      applyLiveSample(data);
    } catch (e) {
      state.serverReachable = false;
      state.deviceConnected = false;
      state.liveLevel = 0;
      state.liveMouthOpen = false;
      updateConnectionBadge();
      updateMeterUI();
    } finally {
      if (state.transport === 'poll') {
        pollTimer = setTimeout(pollLevelOnce, state.pollMs);
      }
    }
  }

  function startLiveStream() {
    if (typeof EventSource !== 'function') {
      state.transport = 'poll';
      pollLevelOnce();
      return;
    }
    closeSse();
    stopPolling();
    state.transport = 'sse';
    const es = new EventSource(`${apiBase}/api/pngtuber/stream`);
    sseSource = es;

    es.onopen = () => {
      if (sseSource !== es) return;
      state.serverReachable = true;
      state.transport = 'sse';
      updateConnectionBadge();
    };

    es.onmessage = (event) => {
      if (sseSource !== es) return;
      try {
        const data = JSON.parse(event.data);
        applyLiveSample(data);
      } catch (e) {}
    };

    es.onerror = () => {
      if (sseSource !== es) return;
      state.deviceConnected = false;
      state.liveLevel = 0;
      state.liveMouthOpen = false;
      updateConnectionBadge();
      updateMeterUI();
      const closedState = typeof EventSource.CLOSED === 'number' ? EventSource.CLOSED : 2;
      if (es.readyState === closedState) {
        closeSse();
        state.transport = 'poll';
        pollLevelOnce();
      }
    };
  }

  function bindEvents() {
    if (micSelect) {
      micSelect.addEventListener('change', () => {
        state.micValue = micSelect.value;
        recomputeDirty();
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
      const nextOpen = round3(clamp(rawVal, 0.001, 1.0, state.openThreshold));
      state.openThreshold = nextOpen;
      if (state.closeThreshold > nextOpen) {
        state.closeThreshold = nextOpen;
      }
      recomputeDirty();
      updateThresholdUI();
    };

    const onCloseChange = (rawVal) => {
      const nextClose = round3(clamp(rawVal, 0.0, state.openThreshold, state.closeThreshold));
      state.closeThreshold = nextClose;
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
        try {
          await Promise.all([
            fetchConfig(false),
            fetchAvailableImages(),
            fetchObsInputs(true),
          ]);
          showBanner('サーバーから最新の設定を再読み込みしました。', 'info', 3000);
        } catch (e) {
          showBanner(e.message || '再読み込みに失敗しました。', 'error');
        }
      });
    }
    if (btnSaveToken) {
      btnSaveToken.addEventListener('click', () => {
        const token = (apiTokenInput && apiTokenInput.value || '').trim();
        if (typeof window.setWidgetApiToken === 'function') {
          window.setWidgetApiToken(token);
        }
        if (tokenPanel) tokenPanel.hidden = true;
        showBanner('API トークンを適用しました。再度「設定を保存」またはアップロードを実行してください。', 'info', 4000);
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
      closeSse();
      stopPolling();
    });
  }

  function initOverlayUrlHint() {
    if (!overlayUrlText) return;
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      const basePath = location.pathname.replace(/\/[^/]*$/, '/');
      overlayUrlText.textContent = `${location.origin}${basePath}index.html`;
    }
  }

  async function init() {
    initOverlayUrlHint();
    bindEvents();
    updateThresholdUI();
    updateConnectionBadge();

    try {
      await Promise.all([
        fetchConfig(false),
        fetchAvailableImages(),
        fetchObsInputs(false),
      ]);
    } catch (e) {
      showBanner(e.message || 'サーバーから設定を読み込めませんでした。統合サーバーが起動しているか確認してください。', 'error');
    }

    startLiveStream();
  }

  init();
})();
