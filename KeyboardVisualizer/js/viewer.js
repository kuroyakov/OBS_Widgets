/* ========================================================
 * OBS ブラウザソース専用 ビューワー設定
 * ======================================================== */
const OBS_CONFIG = {
  maxNoteHoldMs: 30000 // 万が一NoteOffを取りこぼした際の自動消灯タイマー (30秒)
};

(() => {
  'use strict';

  const keyboardWrapper = document.getElementById('keyboard-wrapper');
  const whiteKeysContainer = document.getElementById('white-keys');
  const blackKeysContainer = document.getElementById('black-keys');
  const chordDisplayEl = document.getElementById('chord-display');
  const chordModeBadgeEl = document.getElementById('chord-mode-badge');
  const chordNameEl = document.getElementById('chord-name');

  /* ========================================================
   * 88鍵盤のDOM生成 & マッピング
   * ======================================================== */
  const keyElements = new Map();
  const keyLabelElements = new Map();
  const noteSafetyTimers = new Map();
  const activeNotes = new Set();
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
    const label = document.createElement('span');
    label.className = 'note-label';
    if (!isBlackNote(note)) {
      const key = document.createElement('div');
      key.className = 'key-white';
      key.dataset.note = note;
      key.appendChild(label);
      whiteKeysContainer.appendChild(key);
      keyElements.set(note, key);
      keyLabelElements.set(note, label);
      whiteKeyIndex++;
    } else {
      const key = document.createElement('div');
      key.className = 'key-black';
      key.dataset.note = note;

      const offsetRatio = getBlackKeyOffsetRatio(note);
      const leftPercent = (whiteKeyIndex + offsetRatio) * oneWhiteKeyPercent;
      key.style.left = `${leftPercent}%`;

      key.appendChild(label);
      blackKeysContainer.appendChild(key);
      keyElements.set(note, key);
      keyLabelElements.set(note, label);
    }
  }

  /* ========================================================
   * ==== MUSIC ENGINE START ====
   * 音名スペリング & コード判定エンジン (UC-08)
   * 純粋関数のみ (DOM 非依存)。単体検証はこの範囲を抽出して行う。
   * ======================================================== */
  const LETTER_PC = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 };
  const SHARP_ORDER = ['F', 'C', 'G', 'D', 'A', 'E', 'B'];
  const FLAT_ORDER = ['B', 'E', 'A', 'D', 'G', 'C', 'F'];
  const SHARP_NAMES = ['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B'];
  const FLAT_NAMES = ['C', 'D♭', 'D', 'E♭', 'E', 'F', 'G♭', 'G', 'A♭', 'A', 'B♭', 'B'];

  // keySignature は調号の数 (♯ が正、♭ が負、-6..+6)。ダイアトニック音は調号通り、
  // 調外の半音は調号の方向 (♯系はシャープ名、♭系はフラット名、調号なしはシャープ名) で綴る。
  function spellPitchClass(pc, keySignature) {
    const accidental = {};
    if (keySignature > 0) SHARP_ORDER.slice(0, keySignature).forEach(l => { accidental[l] = 1; });
    if (keySignature < 0) FLAT_ORDER.slice(0, -keySignature).forEach(l => { accidental[l] = -1; });
    for (const letter of Object.keys(LETTER_PC)) {
      const p = (((LETTER_PC[letter] + (accidental[letter] || 0)) % 12) + 12) % 12;
      if (p === pc) return letter + (accidental[letter] === 1 ? '♯' : accidental[letter] === -1 ? '♭' : '');
    }
    return (keySignature >= 0 ? SHARP_NAMES : FLAT_NAMES)[pc];
  }

  const TENSION_ORDER = ['♭9', '♯9', '9', '11', '♯11', '♭13', '13'];
  const TENSION_OF_INTERVAL = { 1: '♭9', 2: '9', 3: '♯9', 5: '11', 6: '♯11', 8: '♭13', 9: '13' };

  function renderChordSuffix(family, base, tensions, mode) {
    const used = new Set();
    const hasT = (t) => tensions.includes(t);
    const useAll = () => tensions.forEach(t => used.add(t));
    const parens = (b) => b + '(' + tensions.join(',') + ')';

    if (mode === 'pops') {
      // ポップス系: テンションは 9 の昇格のみ表記し、それ以外は落とす (落とした分は未説明として減点される)
      if (hasT('9')) {
        if (family === 'dom7' && base === '7') { used.add('9'); return { suffix: '9', used }; }
        if (family === 'maj7') { used.add('9'); return { suffix: 'maj9', used }; }
        if (family === 'min7' && base === 'm7') { used.add('9'); return { suffix: 'm9', used }; }
        if (family === 'sixth') { used.add('9'); return { suffix: base + '/9', used }; }
        if (family === 'triad') { used.add('9'); return { suffix: base === 'm' ? 'madd9' : 'add9', used }; }
      }
      if (family === 'triad' && tensions.length === 0) return { suffix: base, used };
      if (family === 'triad') {
        // 三和音なのに落とすテンションがある解釈は、転回形の誤読であることが多い
        // (例: B-D-G を Bm(♭13) と読む)。ジャズ系の三和音括弧表記と同様に大きく減点し、
        // 完全な三和音の転回解釈 (例: G/B) を優先させる (実機報告 2026-11-08)。
        return { suffix: base, used, penalty: 60 };
      }
      if (family === 'sixth' && tensions.length > 0) {
        // 6th も同様: 落とすテンションがある解釈は誤読であることが多い
        // (例: C7/G を Gm6(11) と読む)。9 の昇格 (6/9) は上の分岐で処理済み。
        // テンションなしの素の 6th (C6 等) はここに来ても減点しない。
        return { suffix: base, used, penalty: 60 };
      }
      return { suffix: base, used };
    }

    // ジャズ系
    switch (family) {
      case 'dom7': {
        const altTensionsOk = tensions.every(t => ['♭9', '♯9', '♭13'].includes(t));
        const isAlt = (base === '7' || base === 'aug7' || base === '7♭5')
          && tensions.length >= 1 && altTensionsOk
          && (hasT('♭9') || hasT('♯9'))
          && (hasT('♭13') || base !== '7');
        if (isAlt) { useAll(); return { suffix: '7alt', used }; }
        if (base === '7') {
          if (tensions.length === 1 && hasT('9')) { useAll(); return { suffix: '9', used }; }
          if (tensions.length === 2 && hasT('9') && hasT('13')) { useAll(); return { suffix: '13', used }; }
          if (tensions.length === 0) return { suffix: '7', used };
          useAll(); return { suffix: parens('7'), used };
        }
        if (tensions.length === 0) return { suffix: base, used };
        useAll(); return { suffix: parens(base), used };
      }
      case 'min7': {
        if (base === 'm7' && tensions.every(t => ['9', '11', '13'].includes(t))) {
          if (hasT('13') && hasT('11')) { useAll(); return { suffix: 'm13', used }; }
          if (hasT('11')) { useAll(); return { suffix: 'm11', used }; }
          if (tensions.length === 1 && hasT('9')) { useAll(); return { suffix: 'm9', used }; }
          if (tensions.length === 1 && hasT('13')) { useAll(); return { suffix: 'm7(13)', used }; }
        }
        if (tensions.length === 0) return { suffix: base, used };
        useAll(); return { suffix: parens(base), used };
      }
      case 'maj7': {
        if (tensions.length === 2 && hasT('9') && hasT('13')) {
          useAll(); return { suffix: 'maj13', used };
        }
        if (hasT('9')) {
          const rest = tensions.filter(t => t !== '9');
          used.add('9');
          if (rest.length === 0) return { suffix: 'maj9', used };
          rest.forEach(t => used.add(t));
          return { suffix: 'maj9(' + rest.join(',') + ')', used };
        }
        if (tensions.length === 0) return { suffix: base, used };
        useAll(); return { suffix: parens(base), used };
      }
      case 'sixth': {
        if (tensions.length === 1 && hasT('9')) { useAll(); return { suffix: base + '/9', used }; }
        if (tensions.length === 0) return { suffix: base, used };
        useAll(); return { suffix: parens(base), used };
      }
      case 'triad': {
        if (tensions.length === 1 && hasT('9')) {
          useAll(); return { suffix: base === 'm' ? 'madd9' : 'add9', used };
        }
        if (tensions.length === 0) return { suffix: base, used };
        // 三和音にテンション括弧が付く解釈は、転回形の誤読であることが多いため大きく減点する
        useAll(); return { suffix: parens(base), used, penalty: 60 };
      }
      case 'dim7': {
        return { suffix: base, used };
      }
      default: {
        if (tensions.length === 0) return { suffix: base, used };
        useAll(); return { suffix: parens(base), used };
      }
    }
  }

  // ルート候補1件分のコード解釈を作る。成立しない場合は null。
  function buildChordCandidate(iv, mode) {
    const has = (i) => iv.has(i);
    const explained = new Set([0]);
    let penalty = 0;

    let third = null;
    if (has(4)) { third = 'maj'; explained.add(4); }
    else if (has(3)) { third = 'min'; explained.add(3); }

    let fifth = 'perf';
    if (has(7)) { explained.add(7); }
    else if (has(6)) { fifth = 'dim'; explained.add(6); }
    else if (has(8) && third !== 'min') { fifth = 'aug'; explained.add(8); }
    // 短3度 + 増5度は増三和音ではない (例: E-G-C は Caug ではなく C/E)。
    // 短3度のとき音程8は 5 度ではなく ♭13 テンションとして扱う。

    let seventh = null;
    if (has(11) && !has(10)) { seventh = 'maj7'; explained.add(11); }
    else if (has(10)) { seventh = '7'; explained.add(10); }

    let isDim7 = false;
    if (!seventh && third === 'min' && fifth === 'dim' && has(9)) {
      isDim7 = true; explained.add(9);
    }
    let isSixth = false;
    if (!seventh && !isDim7 && third && has(9)) {
      isSixth = true; explained.add(9);
    }

    let family = null;
    let base = null;
    if (!third) {
      if (has(5)) { family = 'sus4'; base = seventh ? '7sus4' : 'sus4'; explained.add(5); }
      else if (has(2)) { family = 'sus2'; base = seventh ? '7sus2' : 'sus2'; explained.add(2); }
      else if (seventh === '7') { family = 'dom7'; base = '7'; penalty += 1; }
      else if (seventh === 'maj7') { family = 'maj7'; base = 'maj7'; penalty += 1; }
      else return null;
    } else if (isDim7) {
      family = 'dim7'; base = 'dim7';
    } else if (isSixth) {
      family = 'sixth'; base = third === 'maj' ? '6' : 'm6';
    } else if (seventh === '7' && third === 'min') {
      family = 'min7'; base = fifth === 'dim' ? 'm7♭5' : 'm7';
    } else if (seventh === '7') {
      family = 'dom7';
      base = fifth === 'dim' ? '7♭5' : fifth === 'aug' ? 'aug7' : '7';
    } else if (seventh === 'maj7' && third === 'min') {
      family = 'minmaj7'; base = 'm(maj7)';
    } else if (seventh === 'maj7') {
      family = 'maj7'; base = 'maj7';
    } else {
      family = 'triad';
      if (third === 'min') base = fifth === 'dim' ? 'dim' : fifth === 'aug' ? 'aug' : 'm';
      else base = fifth === 'dim' ? '' : fifth === 'aug' ? 'aug' : '';
      if (third === 'maj' && fifth === 'dim') penalty += 1;
    }

    if (mode === 'pops' && (base === '7♭5' || base === 'aug7')) return null;

    const tensions = [];
    let stray = 0;
    for (const i of iv) {
      if (explained.has(i)) continue;
      if (TENSION_OF_INTERVAL[i]) tensions.push(TENSION_OF_INTERVAL[i]);
      else stray += 1;
    }
    tensions.sort((a, b) => TENSION_ORDER.indexOf(a) - TENSION_ORDER.indexOf(b));

    // 誤読ガード: 3 度のない 7th 解釈や 7 度のない sus 解釈に余分な音がある場合は、
    // クラスタ等をコードと誤認しやすいため候補から外す
    if (!third && (family === 'dom7' || family === 'maj7') && (tensions.length > 0 || stray > 0)) {
      return null;
    }
    if ((family === 'sus4' || family === 'sus2') && !seventh && tensions.length > 0) {
      return null;
    }

    const rendered = renderChordSuffix(family, base, tensions, mode);
    const unexplained = stray + (tensions.length - rendered.used.size);
    if (unexplained >= 2) return null;
    return { suffix: rendered.suffix, unexplained, penalty: penalty + (rendered.penalty || 0) };
  }

  // 発音中のピッチクラス集合 + ベース音 (ピッチクラス) からコード名を決める (UC-08 §6)
  function detectChordName(pcSet, bassPc, keySignature, mode) {
    const pcs = Array.from(pcSet);
    if (pcs.length < 2) return null;
    if (pcs.length === 2) {
      const other = pcs.find(p => p !== bassPc);
      const diff = (((other - bassPc) % 12) + 12) % 12;
      if (diff === 7) return spellPitchClass(bassPc, keySignature) + '5';
      if (diff === 5) {
        return spellPitchClass(other, keySignature) + '5/' + spellPitchClass(bassPc, keySignature);
      }
      return null;
    }
    const roots = [bassPc, ...pcs.filter(p => p !== bassPc)];
    let best = null;
    for (const root of roots) {
      const iv = new Set(pcs.map(p => (((p - root) % 12) + 12) % 12));
      const cand = buildChordCandidate(iv, mode);
      if (!cand) continue;
      const score = (root === bassPc ? 0 : 50) + cand.unexplained * 8 + cand.penalty;
      if (!best || score < best.score) best = { root, suffix: cand.suffix, score };
    }
    if (!best) return null;
    let name = spellPitchClass(best.root, keySignature) + best.suffix;
    if (best.root !== bassPc) name += '/' + spellPitchClass(bassPc, keySignature);
    return name;
  }
  /* ==== MUSIC ENGINE END ==== */

  /* ========================================================
   * ノート名・コード名表示の状態 (UC-08)。永続化せず実行時のみ保持する。
   * ======================================================== */
  const displaySettings = {
    noteLabels: false,
    chordDisplay: false,
    chordMode: 'pops',
    keySignature: 0
  };
  let obsInstance = null;

  // 表示状態の送出と制御の受信で共有する BroadcastChannel (同一ブラウザ検証用)。
  // keyboard.js の midiBroadcastChannel と同じ方式で、送受信に同一インスタンスを
  // 使うため、自分の送出を自分で受信することはない。
  let displayBroadcastChannel = null;
  try {
    displayBroadcastChannel = new BroadcastChannel('keyboard_visualizer_channel_v1');
  } catch (e) {}

  function refreshAllNoteLabels() {
    keyLabelElements.forEach((label, note) => {
      label.textContent = spellPitchClass(note % 12, displaySettings.keySignature);
    });
  }

  function refreshChordDisplay() {
    if (!chordDisplayEl) return;
    keyboardWrapper.classList.toggle('show-note-labels', displaySettings.noteLabels);
    if (!displaySettings.chordDisplay) {
      chordDisplayEl.classList.add('is-hidden');
      return;
    }
    chordDisplayEl.classList.remove('is-hidden');
    if (chordModeBadgeEl) {
      chordModeBadgeEl.textContent = displaySettings.chordMode.toUpperCase();
    }
    if (!chordNameEl) return;
    if (activeNotes.size === 0) {
      chordNameEl.textContent = '';
      return;
    }
    const bassNote = Math.min(...activeNotes);
    const pcSet = new Set(Array.from(activeNotes, n => n % 12));
    chordNameEl.textContent =
      detectChordName(pcSet, bassNote % 12, displaySettings.keySignature, displaySettings.chordMode) || '';
  }

  function broadcastDisplayState() {
    const payload = Object.assign({ type: 'KEYBOARD_DISPLAY_STATE' }, displaySettings);
    if (obsInstance) {
      obsInstance.call('BroadcastCustomEvent', {
        eventData: payload
      }).catch(() => {});
    }
    // BroadcastChannel へも送出する (Issue #185 [1-4])。OBS WebSocket を介さない
    // 同一ブラウザ検証でも KEYBOARD_DISPLAY_STATE が同期されるようにするためで、
    // keyboard.js の broadcastKeyboardState と同じ併用方式。
    if (displayBroadcastChannel) {
      try {
        displayBroadcastChannel.postMessage(payload);
      } catch (e) {}
    }
  }

  function handleDisplayControl(data) {
    if (!data || data.type !== 'KEYBOARD_CONTROL') return;
    let changed = false;
    switch (data.action) {
      case 'setNoteLabels':
        if (typeof data.enabled === 'boolean') {
          displaySettings.noteLabels = data.enabled; changed = true;
        }
        break;
      case 'setChordDisplay':
        if (typeof data.enabled === 'boolean') {
          displaySettings.chordDisplay = data.enabled; changed = true;
        }
        break;
      case 'setChordMode':
        if (data.mode === 'pops' || data.mode === 'jazz') {
          displaySettings.chordMode = data.mode; changed = true;
        }
        break;
      case 'setKeySignature':
        if (Number.isInteger(data.keySignature) && data.keySignature >= -6 && data.keySignature <= 6) {
          displaySettings.keySignature = data.keySignature; changed = true;
          refreshAllNoteLabels();
        }
        break;
      case 'requestDisplayState':
        broadcastDisplayState();
        return;
      default:
        return;
    }
    if (changed) {
      refreshChordDisplay();
      broadcastDisplayState();
    }
  }

  function clearNoteTimer(noteNumber) {
    const timer = noteSafetyTimers.get(noteNumber);
    if (timer) {
      clearTimeout(timer);
      noteSafetyTimers.delete(noteNumber);
    }
  }

  function noteOn(noteNumber) {
    const el = keyElements.get(noteNumber);
    if (!el) return;
    el.classList.add('active');
    activeNotes.add(noteNumber);
    refreshChordDisplay();

    // 押しっぱなしロック防止用の安全タイマー
    clearNoteTimer(noteNumber);
    const timer = setTimeout(() => {
      el.classList.remove('active');
      activeNotes.delete(noteNumber);
      noteSafetyTimers.delete(noteNumber);
      refreshChordDisplay();
    }, OBS_CONFIG.maxNoteHoldMs);
    noteSafetyTimers.set(noteNumber, timer);
  }

  function noteOff(noteNumber) {
    clearNoteTimer(noteNumber);
    const el = keyElements.get(noteNumber);
    if (el) el.classList.remove('active');
    if (activeNotes.delete(noteNumber)) refreshChordDisplay();
  }

  function allNotesOff() {
    noteSafetyTimers.forEach(timer => clearTimeout(timer));
    noteSafetyTimers.clear();
    activeNotes.clear();
    keyElements.forEach(el => el.classList.remove('active'));
    refreshChordDisplay();
  }

  function handleMidiEventData(raw) {
    const data = (raw && raw.eventData) || raw;
    if (!data || data.type !== 'KEYBOARD_MIDI') return;

    switch (data.action) {
      case 'noteOn':
        if (typeof data.note === 'number') noteOn(data.note);
        break;
      case 'noteOff':
        if (typeof data.note === 'number') noteOff(data.note);
        break;
      case 'allNotesOff':
        allNotesOff();
        break;
      case 'suspend':
        if (data.suspended) {
          keyboardWrapper.classList.add('suspended');
          allNotesOff();
        } else {
          keyboardWrapper.classList.remove('suspended');
        }
        break;
    }
  }

  function handleCustomEvent(raw) {
    const data = (raw && raw.eventData) || raw;
    if (!data) return;
    if (data.type === 'KEYBOARD_MIDI') {
      handleMidiEventData(raw);
    } else if (data.type === 'KEYBOARD_CONTROL') {
      handleDisplayControl(data);
    }
  }

  /* ========================================================
   * 1. OBS WebSocket 経由で CustomEvent を受信 (共通ラッパー使用)
   * ======================================================== */
  const client = window.OBSWidgetsClient.createManagedObsClient({
    reconnectDelayMs: 3000,
    setupListeners: (obs) => {
      obs.on('CustomEvent', handleCustomEvent);
    },
    onConnected: (instance) => {
      obsInstance = instance;
      broadcastDisplayState();
    },
    onDisconnected: () => {
      obsInstance = null;
      allNotesOff();
    }
  });

  client.connect();

  /* ========================================================
   * 2. BroadcastChannel フォールバック（同一ブラウザ検証用）
   * ======================================================== */
  if (displayBroadcastChannel) {
    displayBroadcastChannel.onmessage = (ev) => handleCustomEvent(ev.data);
  }

  refreshAllNoteLabels();
  refreshChordDisplay();
})();
