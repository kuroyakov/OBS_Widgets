/**
 * PNGTuber オーバーレイ制御スクリプト (PngTuber/js/app.js)
 * - 4パターンのアバター画像（口開閉 × 目開閉）の事前プリロードと無ちらつき切替
 * - SSE (GET /api/pngtuber/stream) による OBS 音量レベル即時受信（フォールバックで HTTP ポーリング）
 * - ヒステリシス・音節切れ目／減衰（dip）閉口・最小開閉時間による自然な口パク制御
 * - ランダム間隔の独立タイマーによる瞬き制御
 * - URLパラメータ ?debug=1 時のみの軽量デバッグオーバーレイ
 * - 将来の表情/ポーズ切替に向けた BroadcastChannel('pngtuber') 受け口
 */
(() => {
  'use strict';

  const SLOTS = [
    'mouth_open_eye_open',
    'mouth_open_eye_closed',
    'mouth_closed_eye_open',
    'mouth_closed_eye_closed',
  ];

  const DEFAULT_CONFIG = {
    images: {
      mouth_open_eye_open: 'images/mouth_open_eye_open.png',
      mouth_open_eye_closed: 'images/mouth_open_eye_closed.png',
      mouth_closed_eye_open: 'images/mouth_closed_eye_open.png',
      mouth_closed_eye_closed: 'images/mouth_closed_eye_closed.png',
    },
    image_labels: {},
    obs_input_name: '',
    matched_input_name: '',
    open_threshold: 0.05,
    close_threshold: 0.025,
    dip_close_ratio: 0.82,
    blink_min_ms: 2200,
    blink_max_ms: 5500,
    poll_ms: 120,
    min_open_ms: 80,
    min_closed_ms: 45,
  };

  const BLINK_DURATION_MIN_MS = 110;
  const BLINK_DURATION_MAX_MS = 140;
  // OBS InputVolumeMeters は ~50ms 毎の平滑 RMS 相当を送るため、二重スムージングで遅延・谷消しを起こさない
  const SMOOTH_ATTACK = 1.0;
  const SMOOTH_RELEASE = 1.0;
  const DIP_MIN_DROP = 0.012;
  const REOPEN_RISE_RATIO = 1.12;
  const REOPEN_MIN_RISE = 0.010;
  const STUCK_CLOSED_REOPEN_MS = 180;
  const CONFIG_RETRY_MS = 1500;
  const INITIAL_LOAD_GATE_TIMEOUT_MS = 3000;

  function resolveApiBase() {
    if (window.OBSWidgetsEnv && typeof window.OBSWidgetsEnv.API_BASE === 'string') {
      return window.OBSWidgetsEnv.API_BASE;
    }
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      return '';
    }
    return 'http://localhost:8080';
  }

  function clamp(num, min, max, fallback) {
    const n = Number(num);
    if (!Number.isFinite(n)) return fallback;
    return Math.max(min, Math.min(max, n));
  }

  function normalizeConfig(raw) {
    const src = (raw && typeof raw === 'object') ? raw : {};
    const rawImages = (src.images && typeof src.images === 'object') ? src.images : {};
    const images = {};
    for (const slot of SLOTS) {
      const val = rawImages[slot];
      images[slot] = (typeof val === 'string') ? val.trim() : DEFAULT_CONFIG.images[slot];
    }
    const openThreshold = clamp(src.open_threshold, 0.0001, 1.0, DEFAULT_CONFIG.open_threshold);
    const closeDefault = Math.min(openThreshold, DEFAULT_CONFIG.close_threshold);
    const closeThreshold = clamp(src.close_threshold, 0.0, openThreshold, closeDefault);
    const dipCloseRatio = clamp(src.dip_close_ratio, 0.0, 0.98, DEFAULT_CONFIG.dip_close_ratio);
    const blinkMinMs = Math.round(clamp(src.blink_min_ms, 200, 60000, DEFAULT_CONFIG.blink_min_ms));
    const blinkMaxMs = Math.round(clamp(src.blink_max_ms, blinkMinMs, 60000, Math.max(blinkMinMs, DEFAULT_CONFIG.blink_max_ms)));
    const pollMs = Math.round(clamp(src.poll_ms, 50, 2000, DEFAULT_CONFIG.poll_ms));
    const minOpenMs = Math.round(clamp(src.min_open_ms, 0, 1000, DEFAULT_CONFIG.min_open_ms));
    const minClosedMs = Math.round(clamp(src.min_closed_ms, 0, 1000, DEFAULT_CONFIG.min_closed_ms));

    const imageLabels = {};
    const rawLabels = (src.image_labels && typeof src.image_labels === 'object') ? src.image_labels : {};
    for (const slot of SLOTS) {
      imageLabels[slot] = typeof rawLabels[slot] === 'string' && rawLabels[slot]
        ? rawLabels[slot]
        : (images[slot] ? images[slot].split('/').pop().split('?')[0] : slot);
    }

    const obsInputName = typeof src.obs_input_name === 'string'
      ? src.obs_input_name.trim()
      : (typeof src.mic_device_name === 'string' ? src.mic_device_name.trim() : DEFAULT_CONFIG.obs_input_name);
    const matchedInputName = typeof src.matched_input_name === 'string'
      ? src.matched_input_name.trim()
      : DEFAULT_CONFIG.matched_input_name;

    return {
      images,
      image_labels: imageLabels,
      obs_input_name: obsInputName,
      matched_input_name: matchedInputName,
      open_threshold: openThreshold,
      close_threshold: closeThreshold,
      dip_close_ratio: dipCloseRatio,
      blink_min_ms: blinkMinMs,
      blink_max_ms: blinkMaxMs,
      poll_ms: pollMs,
      min_open_ms: minOpenMs,
      min_closed_ms: minClosedMs,
    };
  }

  function getActiveSlot(isMouthOpen, isEyeClosed) {
    if (isMouthOpen) {
      return isEyeClosed ? 'mouth_open_eye_closed' : 'mouth_open_eye_open';
    }
    return isEyeClosed ? 'mouth_closed_eye_closed' : 'mouth_closed_eye_open';
  }

  const apiBase = resolveApiBase();
  const urlParams = new URLSearchParams(window.location.search);
  const avatarParam = (urlParams.get('avatar') || '').trim();
  const debugParam = (urlParams.get('debug') || '').trim().toLowerCase();
  const debugEnabled = debugParam === '1' || debugParam === 'true';

  function withAvatarParam(url) {
    if (!avatarParam) return url;
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}avatar=${encodeURIComponent(avatarParam)}`;
  }

  const avatarCanvas = document.getElementById('avatar-canvas');
  const avatarCtx = avatarCanvas && typeof avatarCanvas.getContext === 'function'
    ? avatarCanvas.getContext('2d')
    : null;

  const debugOverlay = document.getElementById('debug-overlay');
  const dbgLevel = document.getElementById('dbg-level');
  const dbgMeterFill = document.getElementById('dbg-meter-fill');
  const dbgMarkerClose = document.getElementById('dbg-marker-close');
  const dbgMarkerOpen = document.getElementById('dbg-marker-open');
  const dbgThresh = document.getElementById('dbg-thresh');
  const dbgMouthEye = document.getElementById('dbg-mouth-eye');
  const dbgImage = document.getElementById('dbg-image');
  const dbgPreload = document.getElementById('dbg-preload');
  const dbgMic = document.getElementById('dbg-mic');
  const dbgBc = document.getElementById('dbg-bc');

  if (debugEnabled && debugOverlay) {
    debugOverlay.hidden = false;
  }

  const state = {
    config: normalizeConfig(DEFAULT_CONFIG),
    configLoaded: false,
    loadedUrls: {},
    slotImages: {},
    slotCanvases: {},
    slotStatus: {
      mouth_open_eye_open: 'idle',
      mouth_open_eye_closed: 'idle',
      mouth_closed_eye_open: 'idle',
      mouth_closed_eye_closed: 'idle',
    },
    displayReady: false,
    gateTimedOut: false,
    renderedSlot: null,
    renderedCanvas: null,
    mouthOpen: false,
    mouthOpenedAt: 0,
    mouthClosedAt: 0,
    closedByDip: false,
    openPeak: 0,
    closedFloor: 0,
    prevLevel: 0,
    consecutiveFalls: 0,
    eyeClosed: false,
    rawLevel: 0,
    smoothedLevel: 0,
    crosstalkSuppressed: false,
    deviceConnected: false,
    serverReachable: false,
    hadPollFailure: false,
    transport: typeof EventSource === 'function' ? 'sse' : 'poll',
    lastDisplayedSlot: 'mouth_closed_eye_open',
    avatarStateName: 'default',
  };

  let minOpenTimer = null;
  let blinkWaitTimer = null;
  let blinkHoldTimer = null;
  let pollTimer = null;
  let sseSource = null;
  let configRetryTimer = null;
  let initialLoadGateTimer = null;

  function resolveAssetUrl(rawUrl) {
    if (!rawUrl) return '';
    if (rawUrl.startsWith('/')) {
      const full = `${apiBase}${rawUrl}`;
      if (avatarParam && rawUrl.startsWith('/api/pngtuber/image/') && !/[?&]avatar=/.test(rawUrl)) {
        return withAvatarParam(full);
      }
      return full;
    }
    return rawUrl;
  }

  function pickDisplaySlot(targetSlot) {
    if (state.slotStatus[targetSlot] === 'loaded') {
      return targetSlot;
    }
    const mouthFallback = state.mouthOpen ? 'mouth_open_eye_open' : 'mouth_closed_eye_open';
    if (state.slotStatus[mouthFallback] === 'loaded') {
      return mouthFallback;
    }
    if (state.slotStatus.mouth_closed_eye_open === 'loaded') {
      return 'mouth_closed_eye_open';
    }
    for (const slot of SLOTS) {
      if (state.slotStatus[slot] === 'loaded') {
        return slot;
      }
    }
    return targetSlot;
  }

  function clearInitialLoadGateTimer() {
    if (initialLoadGateTimer !== null) {
      clearTimeout(initialLoadGateTimer);
      initialLoadGateTimer = null;
    }
  }

  function getPreloadSummary() {
    let loaded = 0;
    let settled = 0;
    const failedSlots = [];
    for (const slot of SLOTS) {
      const status = state.slotStatus[slot];
      if (status === 'loaded') {
        loaded += 1;
        settled += 1;
      } else if (status === 'error' || status === 'missing') {
        settled += 1;
        failedSlots.push(slot);
      }
    }
    return {
      total: SLOTS.length,
      loaded,
      settled,
      failedSlots,
    };
  }

  function evaluateDisplayGate() {
    if (state.displayReady) return false;
    const summary = getPreloadSummary();
    if (summary.settled === summary.total && summary.loaded > 0) {
      clearInitialLoadGateTimer();
      state.displayReady = true;
      state.gateTimedOut = false;
      return true;
    }
    return false;
  }

  function scheduleInitialLoadGateTimeout() {
    if (state.displayReady || initialLoadGateTimer !== null) return;
    initialLoadGateTimer = setTimeout(() => {
      initialLoadGateTimer = null;
      if (!state.displayReady) {
        state.displayReady = true;
        state.gateTimedOut = true;
        render();
      }
    }, INITIAL_LOAD_GATE_TIMEOUT_MS);
  }

  function materializeSlotCanvas(img) {
    const w = img.naturalWidth || img.width || 0;
    const h = img.naturalHeight || img.height || 0;
    if (w <= 0 || h <= 0) return null;
    const offscreen = document.createElement('canvas');
    offscreen.width = w;
    offscreen.height = h;
    const ctx = typeof offscreen.getContext === 'function' ? offscreen.getContext('2d') : null;
    if (!ctx) return null;
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(img, 0, 0, w, h);
    return offscreen;
  }

  function syncCanvasSize() {
    if (!avatarCanvas) return false;
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const rect = typeof avatarCanvas.getBoundingClientRect === 'function'
      ? avatarCanvas.getBoundingClientRect()
      : { width: window.innerWidth || 0, height: window.innerHeight || 0 };
    const cssW = Math.max(1, Math.round(rect.width || window.innerWidth || 1));
    const cssH = Math.max(1, Math.round(rect.height || window.innerHeight || 1));
    const targetW = Math.round(cssW * dpr);
    const targetH = Math.round(cssH * dpr);
    if (avatarCanvas.width !== targetW || avatarCanvas.height !== targetH) {
      avatarCanvas.width = targetW;
      avatarCanvas.height = targetH;
      return true;
    }
    return false;
  }

  function updateDebugOverlay(targetSlot, displaySlot) {
    if (!debugEnabled || !debugOverlay) return;
    const cfg = state.config;
    const scaleMax = Math.max(0.2, cfg.open_threshold * 2.5, 0.1);
    const fillPct = Math.min(100, Math.max(0, (state.smoothedLevel / scaleMax) * 100));
    const closePct = Math.min(100, Math.max(0, (cfg.close_threshold / scaleMax) * 100));
    const openPct = Math.min(100, Math.max(0, (cfg.open_threshold / scaleMax) * 100));

    if (dbgLevel) {
      const supText = state.crosstalkSuppressed ? ' [crosstalk suppressed]' : '';
      dbgLevel.textContent = `${state.smoothedLevel.toFixed(4)} (raw: ${state.rawLevel.toFixed(4)})${supText}`;
    }
    if (dbgMeterFill) {
      dbgMeterFill.style.width = `${fillPct.toFixed(1)}%`;
      dbgMeterFill.classList.toggle('is-speaking', state.mouthOpen);
    }
    if (dbgMarkerClose) {
      dbgMarkerClose.style.left = `${closePct.toFixed(1)}%`;
    }
    if (dbgMarkerOpen) {
      dbgMarkerOpen.style.left = `${openPct.toFixed(1)}%`;
    }
    if (dbgThresh) {
      const dipText = cfg.dip_close_ratio > 0 ? ` (dip ${cfg.dip_close_ratio.toFixed(2)})` : '';
      dbgThresh.textContent = `open ${cfg.open_threshold.toFixed(3)} / close ${cfg.close_threshold.toFixed(3)}${dipText}`;
    }
    if (dbgMouthEye) {
      dbgMouthEye.textContent = `${state.mouthOpen ? 'open' : 'closed'} / ${state.eyeClosed ? 'closed' : 'open'}`;
      dbgMouthEye.classList.toggle('is-speaking', state.mouthOpen);
    }
    if (dbgImage) {
      const label = cfg.image_labels[targetSlot] || targetSlot;
      const loadStatus = state.slotStatus[targetSlot] || 'idle';
      if (loadStatus === 'loaded') {
        dbgImage.textContent = `${targetSlot} (${label})`;
      } else if (displaySlot && displaySlot !== targetSlot) {
        dbgImage.textContent = `${targetSlot} [${loadStatus}] -> ${displaySlot}`;
      } else {
        dbgImage.textContent = `${targetSlot} [${loadStatus}]`;
      }
    }
    if (dbgPreload) {
      const summary = getPreloadSummary();
      if (!state.displayReady) {
        dbgPreload.textContent = `waiting (${summary.loaded}/${summary.total} loaded)`;
        dbgPreload.classList.remove('is-disconnected');
      } else if (state.gateTimedOut && summary.settled < summary.total) {
        dbgPreload.textContent = `timeout (${summary.loaded}/${summary.total} loaded)`;
        dbgPreload.classList.add('is-disconnected');
      } else if (summary.failedSlots.length > 0) {
        dbgPreload.textContent = `${summary.loaded}/${summary.total} ready (err: ${summary.failedSlots.join(', ')})`;
        dbgPreload.classList.add('is-disconnected');
      } else {
        dbgPreload.textContent = `${summary.loaded}/${summary.total} ready`;
        dbgPreload.classList.remove('is-disconnected');
      }
    }
    if (dbgMic) {
      const targetLabel = cfg.matched_input_name || cfg.obs_input_name || 'obs auto';
      if (!state.serverReachable) {
        dbgMic.textContent = 'server unreachable';
        dbgMic.classList.add('is-disconnected');
      } else if (!state.deviceConnected) {
        dbgMic.textContent = `disconnected (${cfg.obs_input_name || 'obs auto'}) [${state.transport}]`;
        dbgMic.classList.add('is-disconnected');
      } else {
        dbgMic.textContent = `connected (${targetLabel}) [${state.transport}]`;
        dbgMic.classList.remove('is-disconnected');
      }
    }
    if (dbgBc) {
      dbgBc.textContent = state.avatarStateName;
    }
  }

  function render() {
    const targetSlot = getActiveSlot(state.mouthOpen, state.eyeClosed);
    const displaySlot = state.displayReady ? pickDisplaySlot(targetSlot) : null;
    state.lastDisplayedSlot = targetSlot;

    if (avatarCanvas && avatarCtx) {
      const resized = syncCanvasSize();
      const srcCanvas = displaySlot && state.slotStatus[displaySlot] === 'loaded'
        ? (state.slotCanvases[displaySlot] || null)
        : null;

      if (!srcCanvas) {
        if (resized || state.renderedSlot !== null || state.renderedCanvas !== null) {
          avatarCtx.clearRect(0, 0, avatarCanvas.width, avatarCanvas.height);
          state.renderedSlot = null;
          state.renderedCanvas = null;
        }
      } else if (resized || state.renderedSlot !== displaySlot || state.renderedCanvas !== srcCanvas) {
        const cw = avatarCanvas.width;
        const ch = avatarCanvas.height;
        const sw = srcCanvas.width;
        const sh = srcCanvas.height;
        avatarCtx.clearRect(0, 0, cw, ch);
        if (cw > 0 && ch > 0 && sw > 0 && sh > 0) {
          const scale = Math.min(cw / sw, ch / sh);
          const dw = sw * scale;
          const dh = sh * scale;
          const dx = (cw - dw) / 2;
          const dy = (ch - dh) / 2;
          avatarCtx.imageSmoothingEnabled = true;
          if ('imageSmoothingQuality' in avatarCtx) {
            avatarCtx.imageSmoothingQuality = 'high';
          }
          avatarCtx.drawImage(srcCanvas, dx, dy, dw, dh);
        }
        state.renderedSlot = displaySlot;
        state.renderedCanvas = srcCanvas;
      }
    }

    updateDebugOverlay(targetSlot, displaySlot);
  }

  function preloadSlot(slot, rawUrl) {
    const resolved = resolveAssetUrl(rawUrl);
    if (!resolved) {
      state.loadedUrls[slot] = '';
      state.slotImages[slot] = null;
      state.slotCanvases[slot] = null;
      state.slotStatus[slot] = 'missing';
      evaluateDisplayGate();
      render();
      return;
    }
    if (state.loadedUrls[slot] === resolved && state.slotStatus[slot] === 'loaded' && state.slotCanvases[slot]) {
      return;
    }
    state.loadedUrls[slot] = resolved;
    state.slotStatus[slot] = 'loading';
    state.slotCanvases[slot] = null;

    const img = new Image();
    img.decoding = 'async';
    state.slotImages[slot] = img;

    const onReady = () => {
      if (state.loadedUrls[slot] !== resolved || state.slotImages[slot] !== img) return;
      if (state.slotStatus[slot] === 'loaded') return;
      const offscreen = materializeSlotCanvas(img);
      if (!offscreen) {
        state.slotStatus[slot] = 'error';
        state.slotCanvases[slot] = null;
      } else {
        state.slotCanvases[slot] = offscreen;
        state.slotStatus[slot] = 'loaded';
      }
      evaluateDisplayGate();
      render();
    };

    const onError = () => {
      if (state.loadedUrls[slot] !== resolved || state.slotImages[slot] !== img) return;
      if (state.slotStatus[slot] === 'error') return;
      state.slotStatus[slot] = 'error';
      state.slotCanvases[slot] = null;
      evaluateDisplayGate();
      render();
    };

    img.onload = () => {
      if (typeof img.decode === 'function') {
        img.decode().then(onReady, onReady);
      } else {
        onReady();
      }
    };
    img.onerror = onError;
    img.src = resolved;
    if (img.complete && img.naturalWidth > 0) {
      if (typeof img.decode === 'function') {
        img.decode().then(onReady, onReady);
      } else {
        onReady();
      }
    }
  }

  function preloadAllImages() {
    for (const slot of SLOTS) {
      preloadSlot(slot, state.config.images[slot]);
    }
    evaluateDisplayGate();
    if (!state.displayReady) {
      scheduleInitialLoadGateTimeout();
    }
    render();
  }

  function clearMinOpenTimer() {
    if (minOpenTimer !== null) {
      clearTimeout(minOpenTimer);
      minOpenTimer = null;
    }
  }

  function closeMouth(nowMs, level, byDip) {
    clearMinOpenTimer();
    state.mouthOpen = false;
    state.mouthClosedAt = nowMs;
    state.closedByDip = Boolean(byDip);
    state.closedFloor = level;
    state.openPeak = 0;
    state.consecutiveFalls = 0;
  }

  function openMouth(nowMs, level) {
    clearMinOpenTimer();
    state.mouthOpen = true;
    state.mouthOpenedAt = nowMs;
    state.closedByDip = false;
    state.openPeak = level;
    state.closedFloor = level;
    state.consecutiveFalls = 0;
  }

  function shouldCloseMouth(level) {
    const cfg = state.config;
    if (!state.deviceConnected || level <= cfg.close_threshold) {
      return { close: true, byDip: false };
    }
    // コンプレッサー常用時でも音節切れ目（ピーク比低下や連続減衰）で一瞬口を閉じる
    if (cfg.dip_close_ratio > 0 && state.openPeak > 0) {
      const dipDrop = state.openPeak - level;
      const ratioHit = level <= state.openPeak * cfg.dip_close_ratio && dipDrop >= DIP_MIN_DROP;
      const trendHit = state.consecutiveFalls >= 2 && level <= state.openPeak * 0.90 && dipDrop >= DIP_MIN_DROP;
      if (ratioHit || trendHit) {
        return { close: true, byDip: true };
      }
    }
    return { close: false, byDip: false };
  }

  function scheduleDelayedMouthClose(remainingMs) {
    clearMinOpenTimer();
    minOpenTimer = setTimeout(() => {
      minOpenTimer = null;
      if (!state.mouthOpen) return;
      const verdict = shouldCloseMouth(state.smoothedLevel);
      if (verdict.close) {
        closeMouth(performance.now(), state.smoothedLevel, verdict.byDip);
        render();
      }
    }, Math.max(1, remainingMs));
  }

  function processLevelSample(sample, nowMs) {
    const raw = clamp(sample && sample.level, 0.0, 1.0, 0.0);
    const connected = Boolean(sample && sample.device_connected);
    state.deviceConnected = connected;
    state.crosstalkSuppressed = connected && Boolean(sample && sample.crosstalk_suppressed);

    if (!connected) {
      state.rawLevel = 0.0;
      state.smoothedLevel = 0.0;
      state.prevLevel = 0.0;
      state.consecutiveFalls = 0;
      state.closedByDip = false;
      state.closedFloor = 0.0;
      if (state.mouthOpen) {
        const elapsed = nowMs - state.mouthOpenedAt;
        if (elapsed >= state.config.min_open_ms) {
          closeMouth(nowMs, 0.0, false);
        } else {
          scheduleDelayedMouthClose(state.config.min_open_ms - elapsed);
        }
      }
      render();
      return;
    }

    const prev = state.prevLevel;
    state.rawLevel = raw;
    const alpha = raw >= state.smoothedLevel ? SMOOTH_ATTACK : SMOOTH_RELEASE;
    const level = state.smoothedLevel + alpha * (raw - state.smoothedLevel);
    state.smoothedLevel = level;
    state.prevLevel = level;

    if (!state.mouthOpen) {
      if (level <= state.config.close_threshold) {
        state.closedByDip = false;
        state.closedFloor = level;
      } else {
        state.closedFloor = Math.min(state.closedFloor, level);
      }

      if (level >= state.config.open_threshold) {
        if (!state.closedByDip) {
          openMouth(nowMs, level);
        } else {
          const closedElapsed = nowMs - state.mouthClosedAt;
          if (closedElapsed >= state.config.min_closed_ms) {
            const riseThreshold = Math.max(state.config.open_threshold, state.closedFloor * REOPEN_RISE_RATIO);
            const rebounded = level >= riseThreshold && (level - state.closedFloor) >= REOPEN_MIN_RISE;
            const sustainedHold = closedElapsed >= STUCK_CLOSED_REOPEN_MS
              && level >= Math.max(state.config.open_threshold * 1.4, 0.06)
              && level >= prev - 0.003;
            if (rebounded || sustainedHold) {
              openMouth(nowMs, level);
            }
          }
        }
      }
    } else {
      if (level > state.openPeak) {
        state.openPeak = level;
      }
      if (level < prev - 0.002) {
        state.consecutiveFalls += 1;
      } else if (level > prev + 0.002) {
        state.consecutiveFalls = 0;
      }

      const verdict = shouldCloseMouth(level);
      if (verdict.close) {
        const elapsed = nowMs - state.mouthOpenedAt;
        if (elapsed >= state.config.min_open_ms) {
          closeMouth(nowMs, level, verdict.byDip);
        } else {
          scheduleDelayedMouthClose(state.config.min_open_ms - elapsed);
        }
      } else {
        clearMinOpenTimer();
      }
    }

    render();
  }

  function stopBlinkTimers() {
    if (blinkWaitTimer !== null) {
      clearTimeout(blinkWaitTimer);
      blinkWaitTimer = null;
    }
    if (blinkHoldTimer !== null) {
      clearTimeout(blinkHoldTimer);
      blinkHoldTimer = null;
    }
    state.eyeClosed = false;
  }

  function scheduleNextBlink() {
    stopBlinkTimers();
    const minMs = state.config.blink_min_ms;
    const maxMs = Math.max(minMs, state.config.blink_max_ms);
    const delayMs = minMs + Math.random() * (maxMs - minMs);
    blinkWaitTimer = setTimeout(() => {
      blinkWaitTimer = null;
      state.eyeClosed = true;
      render();
      const holdMs = BLINK_DURATION_MIN_MS + Math.random() * (BLINK_DURATION_MAX_MS - BLINK_DURATION_MIN_MS);
      blinkHoldTimer = setTimeout(() => {
        blinkHoldTimer = null;
        state.eyeClosed = false;
        render();
        scheduleNextBlink();
      }, holdMs);
    }, delayMs);
  }

  function applyConfig(rawCfg) {
    const prevMin = state.config.blink_min_ms;
    const prevMax = state.config.blink_max_ms;
    state.config = normalizeConfig(rawCfg);
    state.configLoaded = true;
    preloadAllImages();
    if (prevMin !== state.config.blink_min_ms || prevMax !== state.config.blink_max_ms || (blinkWaitTimer === null && blinkHoldTimer === null)) {
      scheduleNextBlink();
    }
    render();
  }

  async function fetchWithTimeout(url, timeoutMs) {
    if (typeof AbortController === 'function') {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      try {
        return await fetch(url, { cache: 'no-store', signal: ctrl.signal });
      } finally {
        clearTimeout(timer);
      }
    }
    return fetch(url, { cache: 'no-store' });
  }

  async function loadConfig() {
    try {
      const res = await fetchWithTimeout(withAvatarParam(`${apiBase}/api/pngtuber/config`), 1500);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      applyConfig(data);
      return true;
    } catch (e) {
      return false;
    }
  }

  function scheduleConfigRetry() {
    if (configRetryTimer !== null) return;
    configRetryTimer = setTimeout(async () => {
      configRetryTimer = null;
      const ok = await loadConfig();
      if (!ok) {
        scheduleConfigRetry();
      }
    }, CONFIG_RETRY_MS);
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

  function handleIncomingLevelSample(data) {
    state.serverReachable = true;
    const wasConnected = state.deviceConnected;
    const shouldReloadForName = !wasConnected && Boolean(data && data.device_connected)
      && !state.config.matched_input_name;
    if (state.hadPollFailure || !state.configLoaded || shouldReloadForName) {
      state.hadPollFailure = false;
      loadConfig();
    }
    processLevelSample(data, performance.now());
  }

  async function pollLevelOnce() {
    const timeoutMs = Math.max(1000, state.config.poll_ms * 4);
    try {
      const res = await fetchWithTimeout(withAvatarParam(`${apiBase}/api/pngtuber/level`), timeoutMs);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      handleIncomingLevelSample(data);
    } catch (e) {
      state.serverReachable = false;
      state.hadPollFailure = true;
      processLevelSample({ level: 0, device_connected: false }, performance.now());
    } finally {
      if (state.transport === 'poll') {
        pollTimer = setTimeout(pollLevelOnce, state.config.poll_ms);
      }
    }
  }

  function startSseStream() {
    if (typeof EventSource !== 'function') {
      state.transport = 'poll';
      pollLevelOnce();
      return;
    }
    closeSse();
    stopPolling();
    state.transport = 'sse';
    const es = new EventSource(withAvatarParam(`${apiBase}/api/pngtuber/stream`));
    sseSource = es;

    es.onopen = () => {
      if (sseSource !== es) return;
      state.serverReachable = true;
      state.transport = 'sse';
      if (!state.configLoaded || state.hadPollFailure) {
        state.hadPollFailure = false;
        loadConfig();
      }
      render();
    };

    es.onmessage = (event) => {
      if (sseSource !== es) return;
      try {
        const data = JSON.parse(event.data);
        handleIncomingLevelSample(data);
      } catch (e) {}
    };

    es.onerror = () => {
      if (sseSource !== es) return;
      state.serverReachable = false;
      state.hadPollFailure = true;
      processLevelSample({ level: 0, device_connected: false }, performance.now());
      // HTTP 404/503 等で SSE が閉鎖された場合は従来のポーリング方式へ自動フォールバックする
      const closedState = typeof EventSource.CLOSED === 'number' ? EventSource.CLOSED : 2;
      if (es.readyState === closedState) {
        closeSse();
        state.transport = 'poll';
        pollLevelOnce();
      }
    };
  }

  function initStateChannel() {
    if (typeof BroadcastChannel !== 'function') return;
    try {
      const bc = new BroadcastChannel('pngtuber');
      bc.onmessage = (event) => {
        const msg = event && event.data;
        if (!msg || typeof msg !== 'object') return;
        if (msg.type === 'state' && typeof msg.name === 'string') {
          const nextName = msg.name.trim();
          if (nextName) {
            state.avatarStateName = nextName;
            render();
          }
        }
      };
    } catch (e) {}
  }

  async function init() {
    initStateChannel();
    render();
    scheduleNextBlink();
    const ok = await loadConfig();
    if (!ok) {
      preloadAllImages();
      scheduleConfigRetry();
    }
    startSseStream();
  }

  if (typeof window.addEventListener === 'function') {
    window.addEventListener('resize', () => {
      if (syncCanvasSize()) {
        render();
      }
    });
    window.addEventListener('beforeunload', () => {
      closeSse();
      stopPolling();
      clearInitialLoadGateTimer();
    });
  }

  // デバッグ・自動テスト用の軽量フック（DOM 振る舞い自体には干渉しない）
  window.__PNGTUBER_DEBUG__ = {
    getActiveSlot,
    normalizeConfig,
    processLevelSample,
    getState: () => ({
      avatar: avatarParam || 'default',
      displayReady: state.displayReady,
      gateTimedOut: state.gateTimedOut,
      renderedSlot: state.renderedSlot,
      mouthOpen: state.mouthOpen,
      eyeClosed: state.eyeClosed,
      rawLevel: state.rawLevel,
      smoothedLevel: state.smoothedLevel,
      closedByDip: state.closedByDip,
      openPeak: state.openPeak,
      deviceConnected: state.deviceConnected,
      serverReachable: state.serverReachable,
      transport: state.transport,
      avatarStateName: state.avatarStateName,
      lastDisplayedSlot: state.lastDisplayedSlot,
      slotStatus: { ...state.slotStatus },
    }),
  };

  init();
})();
