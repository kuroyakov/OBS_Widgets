// ========================================================
// MixConsole ロータリーノブ制御・メーター表示・フォーマット共通ユーティリティ (js/rotary-and-utils.js)
// ========================================================
'use strict';

  // LUFS から対応する RMS 振幅へ変換 (LUFS = 20 * log10(rms) - 0.691)
  function lufsToRms(lufs) {
    return Math.pow(10, (lufs + 0.691) / 20);
  }

  // タブへ安全にメッセージ送信（タイムアウト付き Promise）
  function requestTabMeterData(tabId, timeoutMs = 250) {
    return new Promise((resolve) => {
      let done = false;
      const timer = setTimeout(() => {
        if (!done) {
          done = true;
          resolve(null);
        }
      }, timeoutMs);

      try {
        chrome.tabs.sendMessage(tabId, { type: 'GET_METER_DATA' }, (res) => {
          clearTimeout(timer);
          if (done) return;
          done = true;
          if (chrome.runtime.lastError || !res || !res.success) {
            resolve(null);
          } else {
            resolve(res);
          }
        });
      } catch (e) {
        clearTimeout(timer);
        if (!done) {
          done = true;
          resolve(null);
        }
      }
    });
  }

  function resetMeterUI(t) {
    if (t.lufsVal) {
      t.lufsVal.textContent = '-∞';
      t.lufsVal.className = 'lufs-num';
    }
    if (t.meterBar) {
      t.meterBar.style.height = '0%';
    }
  }

  function updateTrackMeterUI(t, lufs, peak) {
    if (!t.lufsVal || !t.meterBar) return;

    if (lufs <= -65) {
      t.lufsVal.textContent = '-∞';
      t.lufsVal.className = 'lufs-num';
    } else {
      setLufsDisplay(t.lufsVal, lufs);
    }

    const heightPct = Math.min(100, Math.max(0, Math.round(peak * 100)));
    t.meterBar.style.height = `${heightPct}%`;
  }

  function setLufsDisplay(el, lufs) {
    el.textContent = lufs.toFixed(1);
    // 配信快適スイートスポット (-16.5 〜 -13.0 LUFS) はシアンに点灯
    if (lufs >= -16.5 && lufs <= -13.0) {
      el.className = 'lufs-num sweet';
    } else if (lufs > -11.0) {
      el.className = 'lufs-num hot';
    } else if (lufs > -13.0) {
      el.className = 'lufs-num warm';
    } else {
      el.className = 'lufs-num';
    }
  }


  // --- Arturia Pre 1973 風 同軸ノブ & コントロール ロジック ---
  let rotaryControls = [];

  function setupRotaryControl({
    element,
    getValue,
    setValue,
    min,
    max,
    step,
    defaultValue,
    isLog = false,
    sensitivity = 1.0,
    angleMin = -135,
    angleMax = 135,
    applyTransform,
    onUpdate
  }) {
    if (!element) return { refresh: () => {} };

    function calculateAngle(val) {
      let norm;
      if (isLog) {
        const minLog = Math.log10(min);
        const maxLog = Math.log10(max);
        norm = (Math.log10(val) - minLog) / (maxLog - minLog);
      } else {
        norm = (val - min) / (max - min);
      }
      norm = Math.max(0, Math.min(1, norm));
      return angleMin + norm * (angleMax - angleMin);
    }

    function valueFromNorm(norm) {
      norm = Math.max(0, Math.min(1, norm));
      let val;
      if (isLog) {
        const minLog = Math.log10(min);
        const maxLog = Math.log10(max);
        val = Math.pow(10, minLog + norm * (maxLog - minLog));
      } else {
        val = min + norm * (max - min);
      }
      if (step) {
        val = Math.round(val / step) * step;
      }
      return Math.max(min, Math.min(max, val));
    }

    function refresh() {
      const val = getValue();
      const angle = calculateAngle(val);
      applyTransform(element, angle);
    }

    let isDragging = false;
    let startY = 0;
    let startNorm = 0;

    element.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      e.stopPropagation();
      isDragging = true;
      startY = e.clientY;
      const currentVal = getValue();
      if (isLog) {
        const minLog = Math.log10(min);
        const maxLog = Math.log10(max);
        startNorm = (Math.log10(currentVal) - minLog) / (maxLog - minLog);
      } else {
        startNorm = (currentVal - min) / (max - min);
      }
      try { element.setPointerCapture(e.pointerId); } catch (_) {}
      document.body.style.cursor = 'ns-resize';
    });

    element.addEventListener('pointermove', (e) => {
      if (!isDragging) return;
      e.preventDefault();
      const deltaY = startY - e.clientY;
      const deltaNorm = (deltaY / 150) * sensitivity;
      const newVal = valueFromNorm(startNorm + deltaNorm);
      setValue(newVal);
      refresh();
      if (onUpdate) onUpdate(newVal);
    });

    const stopDragging = (e) => {
      if (!isDragging) return;
      isDragging = false;
      document.body.style.cursor = '';
      try { element.releasePointerCapture(e.pointerId); } catch (_) {}
    };

    element.addEventListener('pointerup', stopDragging);
    element.addEventListener('pointercancel', stopDragging);

    // ダブルクリック: 既定値へ瞬時リセット
    element.addEventListener('dblclick', (e) => {
      e.preventDefault();
      e.stopPropagation();
      setValue(defaultValue);
      refresh();
      if (onUpdate) onUpdate(defaultValue);
    });

    // マウスホイール: 微調整
    element.addEventListener('wheel', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const dir = -Math.sign(e.deltaY);
      const currentVal = getValue();
      let newVal;
      if (isLog) {
        const minLog = Math.log10(min);
        const maxLog = Math.log10(max);
        let norm = (Math.log10(currentVal) - minLog) / (maxLog - minLog);
        norm += dir * 0.02;
        newVal = valueFromNorm(norm);
      } else {
        newVal = Math.max(min, Math.min(max, currentVal + dir * step));
        if (step) newVal = Math.round(newVal / step) * step;
      }
      setValue(newVal);
      refresh();
      if (onUpdate) onUpdate(newVal);
    }, { passive: false });

    const ctrl = { refresh };
    rotaryControls.push(ctrl);
    return ctrl;
  }


  function formatHpf(val) {
    return (!val || val === 0) ? 'OFF' : `${val} Hz`;
  }

  function formatGain(val) {
    const s = val > 0 ? `+${val.toFixed(1)}` : val.toFixed(1);
    return `${s} dB`;
  }

  function formatFreq(val) {
    if (val >= 1000) {
      return `${(val / 1000).toFixed(1)} kHz`;
    }
    return `${Math.round(val)} Hz`;
  }

  function formatQ(val) {
    return val.toFixed(1);
  }


  function setConnectedUI(btn) {
    btn.textContent = 'ACT';
    btn.classList.add('active');
  }

  function setOffUI(btn) {
    btn.textContent = 'OFF';
    btn.classList.remove('active');
  }

  function formatPan(val) {
    if (Math.abs(val) < 0.04) return 'C';
    if (val < 0) return `L${Math.round(Math.abs(val) * 100)}`;
    return `R${Math.round(val * 100)}`;
  }

  function sendUpdate(tabId, params) {
    chrome.tabs.sendMessage(tabId, {
      type: 'UPDATE_PARAMS',
      ...params
    }).catch(() => {});
  }
