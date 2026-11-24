const wsHost = (location.hostname && location.hostname !== '') ? location.hostname : 'localhost';
const OBS_CONFIG = {
  // HTTPS ページでは env-config が返すトンネル経由のアドレスを使う (Issue #115)
  address: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_DEFAULT_ADDRESS) || `ws://${wsHost}:4455`,
  password: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_DEFAULT_PASSWORD) || '',
  autoSyncIntervalMs: 1000,
  // 参照表示用の取得上限解像度 (Issue #142 / P-07)。
  // GetSourceScreenshot の imageWidth/Height は scale-to-inner (アスペクト比維持) のため歪まない
  screenshotMaxWidth: 960,
  screenshotMaxHeight: 540
};

// ソース選択の永続化キー（前回選択・お気に入りピン留め・映像のみ絞り込みモード）
const SOURCE_STORAGE_KEYS = {
  selected: 'telestrator_selected_source',
  favorites: 'telestrator_favorite_sources_v1',
  filterCaptureOnly: 'telestrator_filter_capture_only_v1'
};

(() => {
  'use strict';

  const canvasContainer = document.getElementById('canvas-container');
  const aspectBox = document.getElementById('aspect-box');
  const bgImage = document.getElementById('bg-image');
  const canvas = document.getElementById('draw-canvas');
  const ctx = canvas.getContext('2d');
  const previewCanvas = document.getElementById('preview-canvas');
  const previewCtx = previewCanvas.getContext('2d');

  const statusPill = document.getElementById('status-pill');
  const btnApiToken = document.getElementById('btn-api-token');
  const tokenModal = document.getElementById('token-modal');
  const tokenModalForm = document.getElementById('token-modal-form');
  const apiTokenInput = document.getElementById('api-token-input');
  const tokenModalMsg = document.getElementById('token-modal-msg');
  const btnTokenClose = document.getElementById('btn-token-close');
  const selectSource = document.getElementById('select-source');
  const btnFavSource = document.getElementById('btn-fav-source');
  const btnFilterSource = document.getElementById('btn-filter-source');

  const btnAutoSync = document.getElementById('btn-auto-sync');
  const btnCapture = document.getElementById('btn-capture');
  const btnClear = document.getElementById('btn-clear');
  const btnUndo = document.getElementById('btn-undo');
  const btnRedo = document.getElementById('btn-redo');

  const toolPen = document.getElementById('tool-pen');
  const toolArrow = document.getElementById('tool-arrow');
  const toolRect = document.getElementById('tool-rect');
  const toolEllipse = document.getElementById('tool-ellipse');
  const toolBtns = [toolPen, toolArrow, toolRect, toolEllipse];

  const widthBtns = document.querySelectorAll('.btn-width');
  const colorBtns = document.querySelectorAll('.color-btn');

  let obs = null;
  let isConnected = false;
  let rawObsInputs = [];
  let tokenModalAutoShown = false;
  let tokenApplyPending = false;

  let currentTool = 'pen';
  let currentColor = '#ef4444';
  let currentWidth = 8;

  let history = [];
  let redoStack = [];

  let isDrawing = false;
  let currentStrokePoints = [];
  let startPoint = null;
  let lastPoint = null;

  let isAutoSyncEnabled = true;
  let isFetchingScreenshot = false;

  /* ========================================================
   * キャプチャソースのスマート判定・お気に入り・フィルタ管理
   * ======================================================== */
  function loadFavoriteSources() {
    try {
      const raw = localStorage.getItem(SOURCE_STORAGE_KEYS.favorites);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) return parsed.filter(x => typeof x === 'string' && x.trim() !== '');
      }
    } catch (_) {}
    return [];
  }

  function saveFavoriteSources(list) {
    try {
      localStorage.setItem(SOURCE_STORAGE_KEYS.favorites, JSON.stringify(Array.from(new Set(list))));
    } catch (_) {}
  }

  function loadFilterCaptureOnly() {
    try {
      const val = localStorage.getItem(SOURCE_STORAGE_KEYS.filterCaptureOnly);
      // デフォルトは true（映像キャプチャ＆お気に入りのみ表示）
      return val === null ? true : val === '1';
    } catch (_) {
      return true;
    }
  }

  function saveFilterCaptureOnly(enabled) {
    try {
      localStorage.setItem(SOURCE_STORAGE_KEYS.filterCaptureOnly, enabled ? '1' : '0');
    } catch (_) {}
  }

  let favoriteSources = loadFavoriteSources();
  let filterCaptureOnly = loadFilterCaptureOnly();

  /**
   * OBS の input オブジェクトが「ゲーム画面・映像キャプチャ系ソース」かどうかを判定する。
   * マイク・BGM・ブラウザオーバーレイ・テキスト・PNGTuberアバター(設定されたアバターのソース名等)は除外する。
   */
  function isVideoCaptureSource(input) {
    if (!input || !input.inputName) return false;
    const name = String(input.inputName);
    const nameLower = name.toLowerCase();
    const kindLower = String(input.inputKind || '').toLowerCase();
    const uKindLower = String(input.unversionedInputKind || '').toLowerCase();
    const combinedKind = `${kindLower} ${uKindLower}`;

    // 1. 明確に除外すべき非ゲーム映像ソース（BGM、マイク、ブラウザソース、テキスト、PNGTuberアバター等）
    const pngtuberName = ((window.OBSWidgetsEnv && window.OBSWidgetsEnv.PNGTUBER_SOURCE_NAME) || 'pngtuber').toLowerCase();
    if (
      nameLower === pngtuberName ||
      /veadotube|pngtuber|avatar|アバター|bgm|マイク|mic|audio|音声|commentviewer|コメビュ|ticker|ティッカー|keyboardvisualizer|overlay|オーバーレイ|stamp|スタンプ|endroll|エンドロール|clock|時計/.test(nameLower)
    ) {
      return false;
    }

    if (
      combinedKind.includes('coreaudio') ||
      combinedKind.includes('wasapi') ||
      combinedKind.includes('pulse_') ||
      combinedKind.includes('browser_source') ||
      combinedKind.includes('text_') ||
      combinedKind.includes('color_source') ||
      combinedKind.includes('image_source')
    ) {
      return false;
    }

    // 2. 映像キャプチャデバイス・画面キャプチャ・ゲームキャプチャ系の inputKind
    if (
      combinedKind.includes('av_capture') ||
      combinedKind.includes('avcapture') ||
      combinedKind.includes('screen_capture') ||
      combinedKind.includes('window_capture') ||
      combinedKind.includes('game_capture') ||
      combinedKind.includes('dshow_input') ||
      combinedKind.includes('syphon') ||
      combinedKind.includes('decklink') ||
      combinedKind.includes('ndi_source') ||
      combinedKind.includes('v4l2_input') ||
      combinedKind.includes('display_capture') ||
      combinedKind.includes('monitor_capture')
    ) {
      return true;
    }

    // 3. ソース名にゲーム・キャプチャ系キーワードが含まれる場合
    if (/capture|キャプチャ|game|ゲーム|switch|スプラ|splatoon|elgato|hdmi|画面|映像|main|メイン/.test(nameLower)) {
      return true;
    }

    return false;
  }

  function updateSourceFilterUI() {
    if (btnFilterSource) {
      btnFilterSource.classList.toggle('active', filterCaptureOnly);
      btnFilterSource.textContent = filterCaptureOnly ? '🎯 映像のみ' : '📋 全ソース';
    }
    if (btnFavSource) {
      const currentVal = selectSource.value;
      const isFav = Boolean(currentVal && favoriteSources.includes(currentVal));
      btnFavSource.classList.toggle('active', isFav);
      btnFavSource.textContent = isFav ? '★' : '☆';
    }
  }

  function populateSourceDropdown(inputs, triggerInitialCapture = false) {
    if (!Array.isArray(inputs)) return;
    rawObsInputs = inputs;

    const savedSource = localStorage.getItem(SOURCE_STORAGE_KEYS.selected) || selectSource.value || '';

    // 1. お気に入り(★)、2. 映像キャプチャ系(🎮)、3. その他 に分類
    const favItems = [];
    const captureItems = [];
    const otherItems = [];

    inputs.forEach(inp => {
      if (!inp || !inp.inputName) return;
      const name = inp.inputName;
      if (favoriteSources.includes(name)) {
        favItems.push(inp);
      } else if (isVideoCaptureSource(inp)) {
        captureItems.push(inp);
      } else {
        otherItems.push(inp);
      }
    });

    selectSource.innerHTML = '';

    // もし映像キャプチャ系もお気に入りも1件も見つからない場合は自動的に全件表示へフォールバック
    const hasPrimaryCandidates = (favItems.length + captureItems.length) > 0;
    const showAll = !filterCaptureOnly || !hasPrimaryCandidates;

    const appendOption = (parent, name, prefix = '') => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = prefix ? `${prefix}${name}` : name;
      parent.appendChild(opt);
    };

    favItems.forEach(inp => appendOption(selectSource, inp.inputName, '★ '));
    captureItems.forEach(inp => appendOption(selectSource, inp.inputName, '🎮 '));

    // 前回選択していたソースが「その他」にある場合でも、見失わないよう必ず選択肢に残す
    if (!showAll && savedSource) {
      const savedInOther = otherItems.find(inp => inp.inputName === savedSource);
      if (savedInOther) {
        appendOption(selectSource, savedInOther.inputName, '📌 ');
      }
    }

    if (showAll && otherItems.length > 0) {
      if (hasPrimaryCandidates) {
        const group = document.createElement('optgroup');
        group.label = 'その他のソース (音声・ブラウザ等)';
        otherItems.forEach(inp => appendOption(group, inp.inputName, ''));
        selectSource.appendChild(group);
      } else {
        otherItems.forEach(inp => appendOption(selectSource, inp.inputName, ''));
      }
    }

    // 選択状態の復元（1. 前回選択したソース -> 2. お気に入り先頭 -> 3. 映像キャプチャ先頭）
    const allOptionValues = Array.from(selectSource.options).map(o => o.value);
    if (savedSource && allOptionValues.includes(savedSource)) {
      selectSource.value = savedSource;
    } else if (favItems.length > 0) {
      selectSource.value = favItems[0].inputName;
    } else if (captureItems.length > 0) {
      selectSource.value = captureItems[0].inputName;
    } else if (allOptionValues.length > 0) {
      selectSource.value = allOptionValues[0];
    }

    if (selectSource.value) {
      try {
        localStorage.setItem(SOURCE_STORAGE_KEYS.selected, selectSource.value);
      } catch (_) {}
    }

    updateSourceFilterUI();

    if (triggerInitialCapture && selectSource.value) {
      fetchScreenshot();
    }
  }

  function updateAutoSyncButtonUI() {
    if (!isAutoSyncEnabled) {
      btnAutoSync.textContent = '⏸️ 停止';
      btnAutoSync.className = '';
    } else if (history.length > 0) {
      btnAutoSync.textContent = '🔒 ロック';
      btnAutoSync.className = 'paused';
    } else {
      btnAutoSync.textContent = '🔄 自動';
      btnAutoSync.className = 'active';
    }
  }

  function updateUndoRedoButtons() {
    btnUndo.disabled = history.length === 0;
    btnRedo.disabled = redoStack.length === 0;
    updateAutoSyncButtonUI();
  }


  /* ========================================================
   * 履歴再描画
   * ======================================================== */
  function redrawCanvas() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    previewCtx.clearRect(0, 0, previewCanvas.width, previewCanvas.height);

    history.forEach(item => {
      if (item.type === 'pen' && Array.isArray(item.points)) {
        if (item.points.length < 2) return;
        ctx.beginPath();
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.strokeStyle = item.color;
        ctx.lineWidth = item.width;

        const first = item.points.at(0);
        ctx.moveTo(first.normX * canvas.width, first.normY * canvas.height);

        for (let i = 1; i < item.points.length; i++) {
          const pt = item.points.at(i);
          ctx.lineTo(pt.normX * canvas.width, pt.normY * canvas.height);
        }
        ctx.stroke();
      } else if (item.type === 'arrow') {
        drawArrow(ctx, item.x1 * canvas.width, item.y1 * canvas.height, item.x2 * canvas.width, item.y2 * canvas.height, item.color, item.width);
      } else if (item.type === 'rect') {
        drawRect(ctx, item.x1 * canvas.width, item.y1 * canvas.height, item.x2 * canvas.width, item.y2 * canvas.height, item.color, item.width);
      } else if (item.type === 'ellipse') {
        drawEllipse(ctx, item.x1 * canvas.width, item.y1 * canvas.height, item.x2 * canvas.width, item.y2 * canvas.height, item.color, item.width);
      }
    });

    updateUndoRedoButtons();
  }

  /* ========================================================
   * 16:9 キャンバスサイズ調整
   * ======================================================== */
  function resizeCanvas() {
    const containerW = canvasContainer.clientWidth;
    const containerH = canvasContainer.clientHeight;

    let w = containerW;
    let h = containerW * (9 / 16);

    if (h > containerH) {
      h = containerH;
      w = containerH * (16 / 9);
    }

    aspectBox.style.width = `${w}px`;
    aspectBox.style.height = `${h}px`;

    canvas.width = 1920;
    canvas.height = 1080;

    previewCanvas.width = 1920;
    previewCanvas.height = 1080;

    redrawCanvas();
  }
  window.addEventListener('resize', resizeCanvas);

  function isAuthFailure(err) {
    if (!err) return false;
    if (err.code === 4009) return true;
    const msg = String(err.message || err).toLowerCase();
    return msg.includes('authentication') || msg.includes('password');
  }

  function openTokenModal(message = '') {
    if (!tokenModal) return;
    if (tokenModalMsg) {
      tokenModalMsg.textContent = message;
      tokenModalMsg.hidden = !message;
    }
    tokenModal.hidden = false;
    if (apiTokenInput) {
      apiTokenInput.focus();
    }
  }

  function closeTokenModal() {
    if (!tokenModal) return;
    tokenModal.hidden = true;
    if (tokenModalMsg) {
      tokenModalMsg.textContent = '';
      tokenModalMsg.hidden = true;
    }
  }

  async function maybeShowTokenModalOnFailure(err) {
    if (location.protocol === 'file:') return;
    if (!tokenModal || !tokenModal.hidden) return;
    const authErr = isAuthFailure(err);
    const token = typeof window.getWidgetApiToken === 'function'
      ? await window.getWidgetApiToken()
      : '';
    if (tokenApplyPending) {
      tokenApplyPending = false;
      openTokenModal(authErr
        ? 'OBS 接続の認証に失敗しました。API トークンを確認して再入力してください。'
        : 'OBS へ接続できませんでした。トークンやサーバー状態を確認してください。');
      return;
    }
    if (!tokenModalAutoShown && (!token || authErr)) {
      tokenModalAutoShown = true;
      openTokenModal(authErr
        ? 'OBS WebSocket の認証に失敗しました。API トークンを入力してください。'
        : 'LAN 経由で接続するには API トークンの入力が必要です。');
    }
  }

  /* ========================================================
   * OBS WebSocket 接続 (shared/obs-client.js 共通ラッパー使用)
   * ======================================================== */
  const obsClient = window.OBSWidgetsClient.createManagedObsClient({
    addresses: (window.OBSWidgetsEnv && window.OBSWidgetsEnv.OBS_FALLBACK_ADDRESSES) || [OBS_CONFIG.address],
    password: OBS_CONFIG.password,
    reconnectDelayMs: 3000,
    onConnected: async (connectedObs) => {
      obs = connectedObs;
      isConnected = true;
      tokenApplyPending = false;
      tokenModalAutoShown = false;
      closeTokenModal();
      statusPill.textContent = '接続完了';
      statusPill.className = 'connected';

      const res = await obs.call('GetInputList').catch(() => null);
      if (res?.inputs && Array.isArray(res.inputs)) {
        populateSourceDropdown(res.inputs, true);
      }
    },
    onDisconnected: (err) => {
      isConnected = false;
      statusPill.textContent = '切断 (再接続中)';
      statusPill.className = '';
      maybeShowTokenModalOnFailure(err);
    }
  });

  if (btnApiToken) {
    btnApiToken.addEventListener('click', async () => {
      if (apiTokenInput && typeof window.getWidgetApiToken === 'function') {
        const current = await window.getWidgetApiToken();
        if (current && !apiTokenInput.value) {
          apiTokenInput.value = current;
        }
      }
      openTokenModal('');
    });
  }

  if (btnTokenClose) {
    btnTokenClose.addEventListener('click', () => {
      tokenApplyPending = false;
      closeTokenModal();
    });
  }

  if (tokenModalForm) {
    tokenModalForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const token = (apiTokenInput && apiTokenInput.value || '').trim();
      if (typeof window.setWidgetApiToken === 'function') {
        window.setWidgetApiToken(token);
      }
      if (typeof obsClient.resetPasswordCache === 'function') {
        obsClient.resetPasswordCache();
      }
      tokenApplyPending = true;
      closeTokenModal();
      obsClient.connect();
    });
  }

  function connectOBS() {
    obsClient.connect();
  }

  selectSource.addEventListener('change', () => {
    if (selectSource.value) {
      try {
        localStorage.setItem(SOURCE_STORAGE_KEYS.selected, selectSource.value);
      } catch (_) {}
      updateSourceFilterUI();
      fetchScreenshot();
    }
  });

  if (btnFavSource) {
    btnFavSource.addEventListener('click', () => {
      const cur = selectSource.value;
      if (!cur) return;
      if (favoriteSources.includes(cur)) {
        favoriteSources = favoriteSources.filter(s => s !== cur);
      } else {
        favoriteSources.push(cur);
      }
      saveFavoriteSources(favoriteSources);
      populateSourceDropdown(rawObsInputs, false);
    });
  }

  if (btnFilterSource) {
    btnFilterSource.addEventListener('click', () => {
      filterCaptureOnly = !filterCaptureOnly;
      saveFilterCaptureOnly(filterCaptureOnly);
      populateSourceDropdown(rawObsInputs, false);
    });
  }

  /* ========================================================
   * 画面取り込み関数
   * ======================================================== */
  async function fetchScreenshot() {
    if (!isConnected || !obs) return;
    const targetSource = selectSource.value;
    if (!targetSource) return;

    if (isFetchingScreenshot) return;
    isFetchingScreenshot = true;

    try {
      const res = await obs.call('GetSourceScreenshot', {
        sourceName: targetSource,
        imageFormat: 'jpeg',
        imageWidth: OBS_CONFIG.screenshotMaxWidth,
        imageHeight: OBS_CONFIG.screenshotMaxHeight,
        imageCompressionQuality: 75
      });

      if (res?.imageData) {
        bgImage.src = res.imageData;
      }
    } catch (err) {
    } finally {
      isFetchingScreenshot = false;
    }
  }

  setInterval(() => {
    if (!isAutoSyncEnabled) return;
    if (document.hidden) return;
    if (isDrawing) return;
    if (history.length > 0) return;

    fetchScreenshot();
  }, OBS_CONFIG.autoSyncIntervalMs);

  btnAutoSync.addEventListener('click', () => {
    isAutoSyncEnabled = !isAutoSyncEnabled;
    updateAutoSyncButtonUI();
    if (isAutoSyncEnabled && history.length === 0) {
      fetchScreenshot();
    }
  });

  btnCapture.addEventListener('click', () => {
    fetchScreenshot();
  });

  function sendToOBS(data) {
    if (!isConnected || !obs) return;
    obs.call('BroadcastCustomEvent', { eventData: data }).catch(() => {});
  }

  function syncHistoryToOBS() {
    sendToOBS({
      type: 'sync_history',
      history: history
    });
  }

  /* ========================================================
   * 描画座標計算
   * ======================================================== */
  function getCoordsFromClient(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const normX = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const normY = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));
    return {
      normX,
      normY,
      pxX: normX * canvas.width,
      pxY: normY * canvas.height
    };
  }

  function getStylusTouch(touchList) {
    for (let i = 0; i < touchList.length; i++) {
      const t = touchList.item(i);
      if (t && t.touchType === 'stylus') {
        return t;
      }
    }
    return null;
  }

  /* ========================================================
   * ストローク処理
   * ======================================================== */
  function startStroke(pt) {
    isDrawing = true;
    startPoint = pt;
    lastPoint = pt;
    currentStrokePoints = [pt];

    if (currentTool === 'pen') {
      ctx.beginPath();
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.strokeStyle = currentColor;
      ctx.lineWidth = currentWidth;
      ctx.moveTo(pt.pxX, pt.pxY);
    }
  }

  function moveStroke(pt) {
    if (!isDrawing) return;

    if (currentTool === 'pen') {
      ctx.lineTo(pt.pxX, pt.pxY);
      ctx.stroke();

      currentStrokePoints.push(pt);

      sendToOBS({
        type: 'draw_line',
        x1: lastPoint.normX,
        y1: lastPoint.normY,
        x2: pt.normX,
        y2: pt.normY,
        color: currentColor,
        width: currentWidth
      });

      lastPoint = pt;
    } else if (startPoint) {
      previewCtx.clearRect(0, 0, previewCanvas.width, previewCanvas.height);

      if (currentTool === 'arrow') {
        drawArrow(previewCtx, startPoint.pxX, startPoint.pxY, pt.pxX, pt.pxY, currentColor, currentWidth);
      } else if (currentTool === 'rect') {
        drawRect(previewCtx, startPoint.pxX, startPoint.pxY, pt.pxX, pt.pxY, currentColor, currentWidth);
      } else if (currentTool === 'ellipse') {
        drawEllipse(previewCtx, startPoint.pxX, startPoint.pxY, pt.pxX, pt.pxY, currentColor, currentWidth);
      }
    }
  }

  function endStroke(pt) {
    if (!isDrawing) return;
    isDrawing = false;

    const endPt = pt || lastPoint;

    if (currentTool === 'pen') {
      if (endPt) currentStrokePoints.push(endPt);

      if (currentStrokePoints.length > 0) {
        history.push({
          type: 'pen',
          color: currentColor,
          width: currentWidth,
          points: currentStrokePoints
        });
        redoStack = [];
        syncHistoryToOBS();
        updateUndoRedoButtons();
      }
    } else if (startPoint && endPt) {
      previewCtx.clearRect(0, 0, previewCanvas.width, previewCanvas.height);

      if (currentTool === 'arrow') {
        drawArrow(ctx, startPoint.pxX, startPoint.pxY, endPt.pxX, endPt.pxY, currentColor, currentWidth);
        history.push({
          type: 'arrow',
          color: currentColor,
          width: currentWidth,
          x1: startPoint.normX,
          y1: startPoint.normY,
          x2: endPt.normX,
          y2: endPt.normY
        });
      } else if (currentTool === 'rect') {
        drawRect(ctx, startPoint.pxX, startPoint.pxY, endPt.pxX, endPt.pxY, currentColor, currentWidth);
        history.push({
          type: 'rect',
          color: currentColor,
          width: currentWidth,
          x1: startPoint.normX,
          y1: startPoint.normY,
          x2: endPt.normX,
          y2: endPt.normY
        });
      } else if (currentTool === 'ellipse') {
        drawEllipse(ctx, startPoint.pxX, startPoint.pxY, endPt.pxX, endPt.pxY, currentColor, currentWidth);
        history.push({
          type: 'ellipse',
          color: currentColor,
          width: currentWidth,
          x1: startPoint.normX,
          y1: startPoint.normY,
          x2: endPt.normX,
          y2: endPt.normY
        });
      }

      redoStack = [];
      syncHistoryToOBS();
      updateUndoRedoButtons();
    }

    currentStrokePoints = [];
    startPoint = null;
    lastPoint = null;
  }

  // タッチイベント
  canvas.addEventListener('touchstart', (e) => {
    const stylus = getStylusTouch(e.touches);
    if (!stylus) return;

    e.preventDefault();
    if (isDrawing) endStroke(lastPoint);

    const pt = getCoordsFromClient(stylus.clientX, stylus.clientY);
    startStroke(pt);
  }, { passive: false });

  canvas.addEventListener('touchmove', (e) => {
    const stylus = getStylusTouch(e.touches);
    if (!stylus || !isDrawing) return;

    e.preventDefault();
    const pt = getCoordsFromClient(stylus.clientX, stylus.clientY);
    moveStroke(pt);
  }, { passive: false });

  canvas.addEventListener('touchend', (e) => {
    if (!isDrawing) return;
    const stylus = getStylusTouch(e.changedTouches);
    const pt = stylus ? getCoordsFromClient(stylus.clientX, stylus.clientY) : lastPoint;
    endStroke(pt);
  }, { passive: false });

  canvas.addEventListener('touchcancel', () => {
    if (isDrawing) {
      previewCtx.clearRect(0, 0, previewCanvas.width, previewCanvas.height);
      endStroke(lastPoint);
    }
  });

  // マウス操作
  canvas.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    startStroke(getCoordsFromClient(e.clientX, e.clientY));
  });

  window.addEventListener('mousemove', (e) => {
    if (!isDrawing) return;
    moveStroke(getCoordsFromClient(e.clientX, e.clientY));
  });

  window.addEventListener('mouseup', (e) => {
    if (!isDrawing) return;
    endStroke(getCoordsFromClient(e.clientX, e.clientY));
  });

  /* ========================================================
   * Undo / Redo / Clear
   * ======================================================== */
  btnUndo.addEventListener('click', () => {
    if (history.length === 0) return;
    const popped = history.pop();
    if (popped) {
      redoStack.push(popped);
      redrawCanvas();
      syncHistoryToOBS();
    }
  });

  btnRedo.addEventListener('click', () => {
    if (redoStack.length === 0) return;
    const restored = redoStack.pop();
    if (restored) {
      history.push(restored);
      redrawCanvas();
      syncHistoryToOBS();
    }
  });

  btnClear.addEventListener('click', () => {
    history = [];
    redoStack = [];
    redrawCanvas();
    sendToOBS({ type: 'clear' });
    if (isAutoSyncEnabled) {
      fetchScreenshot();
    }
  });

  /* ========================================================
   * ツール＆カラー＆太さ選択UI
   * ======================================================== */
  function setTool(toolName, activeBtn) {
    currentTool = toolName;
    toolBtns.forEach(btn => btn.classList.remove('active'));
    activeBtn.classList.add('active');
  }

  toolPen.addEventListener('click', () => setTool('pen', toolPen));
  toolArrow.addEventListener('click', () => setTool('arrow', toolArrow));
  toolRect.addEventListener('click', () => setTool('rect', toolRect));
  toolEllipse.addEventListener('click', () => setTool('ellipse', toolEllipse));

  widthBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      widthBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentWidth = Number(btn.dataset.width) || 8;
    });
  });

  colorBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      colorBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentColor = btn.dataset.color;
    });
  });

  updateSourceFilterUI();
  resizeCanvas();
  connectOBS();
})();
