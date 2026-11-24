document.addEventListener('DOMContentLoaded', async () => {
  const container = document.getElementById('mixerRack');
  const masterVolSlider = document.getElementById('masterVol');
  const masterVolVal = document.getElementById('masterVolVal');
  const masterMuteBtn = document.getElementById('masterMute');
  const masterLufsNum = document.getElementById('master-lufs-num');
  const masterMeterBar = document.getElementById('master-meter-bar');

  // 聖徳太子（大判メダリオンボタン）
  const shotokuBtn = document.getElementById('shotokuBtn');

  // モーダル要素 (Arturia Pre 1973 風)
  const eqModal = document.getElementById('eqModal');
  const modalTrackTitle = document.getElementById('modalTrackTitle');
  const modalCloseBtn = document.getElementById('modalCloseBtn');
  const modalDoneBtn = document.getElementById('modalDoneBtn');
  const modalResetBtn = document.getElementById('modalResetBtn');

  const eqLampLed = document.getElementById('eqLampLed');
  const mEqToggle = document.getElementById('m-eq-toggle');
  const mRedKnob = document.getElementById('m-red-knob');
  const mRedVal = document.getElementById('m-red-val');

  // HPF ノブ
  const mHpfKnob = document.getElementById('m-hpf-knob');
  const mEqHpfVal = document.getElementById('m-eq-hpf-val');

  // LOW バンド (GAIN & FREQ 独立ノブ)
  const mLowGainKnob = document.getElementById('m-low-gain-knob');
  const mLowGainVal = document.getElementById('m-eq-low-gain-val');
  const mLowFreqKnob = document.getElementById('m-low-freq-knob');
  const mLowFreqVal = document.getElementById('m-eq-low-freq-val');

  // MID バンド (GAIN & FREQ & Q 独立ノブ)
  const mMidGainKnob = document.getElementById('m-mid-gain-knob');
  const mMidGainVal = document.getElementById('m-eq-mid-gain-val');
  const mMidFreqKnob = document.getElementById('m-mid-freq-knob');
  const mMidFreqVal = document.getElementById('m-eq-mid-freq-val');
  const mMidQKnob = document.getElementById('m-mid-q-knob');
  const mMidQVal = document.getElementById('m-eq-mid-q-val');

  // HIGH バンド (GAIN 独立ノブ)
  const mHiGainKnob = document.getElementById('m-hi-gain-knob');
  const mHiGainVal = document.getElementById('m-eq-hi-gain-val');

  let currentEditingTrack = null;

  let masterVolume = 1.0;
  let isMasterMuted = false;
  let lastActiveMasterVolume = 1.0;

  // マスター状態復元
  const savedMaster = await chrome.storage.local.get(['masterVol', 'masterMuted']);
  if (typeof savedMaster.masterVol === 'number') {
    masterVolume = savedMaster.masterVol;
    lastActiveMasterVolume = masterVolume > 0 ? masterVolume : 1.0;
    masterVolSlider.value = masterVolume;
    masterVolVal.textContent = `${Math.round(masterVolume * 100)}%`;
  }
  if (savedMaster.masterMuted) {
    isMasterMuted = true;
    masterMuteBtn.classList.add('active');
  }

  // 音声再生中のタブを取得
  const tabs = await chrome.tabs.query({ audible: true });
  let targetTabs = tabs;
  if (targetTabs.length === 0) {
    const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true });
    targetTabs = activeTabs.filter(t => t.url && t.url.startsWith('http'));
  }

  if (targetTabs.length === 0) {
    container.innerHTML = '<div class="empty-msg">NO AUDIO SIGNALS DETECTED</div>';
  }

  const trackRegistry = new Map();
  let activeSoloTabId = null;
  const preSoloVolumes = new Map();

  // 各タブのトラック設定キー一覧を取得
  const storageKeys = targetTabs.map(t => `tab_settings_${t.id}`);
  const storedData = await chrome.storage.session.get(storageKeys);

  for (const tab of targetTabs) {
    const strip = document.createElement('div');
    strip.className = 'channel-strip';
    strip.id = `strip-${tab.id}`;

    const title = tab.title || 'Track';
    let tabOrigin = '';
    try {
      if (tab.url) tabOrigin = new URL(tab.url).origin;
    } catch (_) {}
    const storageKey = `tab_settings_${tab.id}`;
    const rawSaved = storedData[storageKey];
    const saved = (rawSaved && rawSaved.origin && rawSaved.origin === tabOrigin) ? rawSaved : {};

    const initialGain = typeof saved.channelGain === 'number' ? saved.channelGain : (saved.volume ?? 1.0);
    const initialPan = typeof saved.pan === 'number' ? saved.pan : 0;
    const initialBypassed = !!saved.bypassed;

    strip.innerHTML = `
      <div class="ch-header-row">
        <button class="connect-btn" id="conn-${tab.id}">OFF</button>
        <button class="eq-edit-btn" id="eq-btn-${tab.id}" title="Channel EQ">e</button>
      </div>

      <div class="pan-box">
        <div class="pan-label-row">
          <span>PAN</span>
          <span class="pan-value" id="pan-val-${tab.id}">${formatPan(initialPan)}</span>
        </div>
        <input type="range" class="pan-slider" id="pan-${tab.id}" min="-1" max="1" step="0.02" value="${initialPan}">
      </div>

      <!-- 平均LUFS表示ボックス (A案) -->
      <div class="lufs-meter-box" id="lufs-box-${tab.id}">
        <span class="lufs-tag">AVG</span>
        <span class="lufs-num" id="lufs-val-${tab.id}">-∞</span>
        <span class="lufs-unit">LUFS</span>
      </div>

      <div class="fader-area">
        <div class="db-scale">
          <span>+3</span>
          <span>0</span>
          <span>-6</span>
          <span>-18</span>
          <span>-∞</span>
        </div>
        <!-- 縦型LEDレベルメーター (B案) -->
        <div class="meter-track">
          <div class="meter-bar" id="meter-${tab.id}"></div>
        </div>
        <input type="range" class="ch-fader" id="vol-${tab.id}" min="0" max="1.4" step="0.02" value="${initialGain}">
      </div>

      <div class="fader-footer">
        <div class="btn-group">
          <button class="mute-btn" id="mute-${tab.id}">M</button>
          <button class="solo-btn" id="solo-${tab.id}">S</button>
        </div>
        <span class="vol-value" id="vol-val-${tab.id}">${Math.round(initialGain * 100)}%</span>
      </div>

      <div class="track-name"></div>
    `;

    // タブタイトルは XSS 防止のため textContent で安全に設定
    const trackNameEl = strip.querySelector('.track-name');
    if (trackNameEl) {
      trackNameEl.textContent = title;
      trackNameEl.title = title;
    }

    container.appendChild(strip);

    const trackObj = {
      tabId: tab.id,
      origin: tabOrigin,
      title: title,
      connBtn: strip.querySelector(`#conn-${tab.id}`),
      eqBtn: strip.querySelector(`#eq-btn-${tab.id}`),
      panSlider: strip.querySelector(`#pan-${tab.id}`),
      volSlider: strip.querySelector(`#vol-${tab.id}`),
      panVal: strip.querySelector(`#pan-val-${tab.id}`),
      volVal: strip.querySelector(`#vol-val-${tab.id}`),
      muteBtn: strip.querySelector(`#mute-${tab.id}`),
      soloBtn: strip.querySelector(`#solo-${tab.id}`),
      lufsVal: strip.querySelector(`#lufs-val-${tab.id}`),
      meterBar: strip.querySelector(`#meter-${tab.id}`),
      channelGain: initialGain,
      lastActiveGain: initialGain > 0 ? initialGain : 1.0,
      bypassed: initialBypassed,
      connected: false,
      // EQ パラメータ (Pre 1973 スタイル)
      eqHpf: saved.eqHpf ?? 0,
      eqHigh: saved.eqHigh ?? 0,
      eqHighFreq: 12000,
      eqMid: saved.eqMid ?? 0,
      eqMidFreq: saved.eqMidFreq ?? 1500,
      eqMidQ: saved.eqMidQ ?? 1.0,
      eqLow: saved.eqLow ?? 0,
      eqLowFreq: saved.eqLowFreq ?? 200,
      eqTrim: saved.eqTrim ?? 0,
      eqBypassed: saved.eqBypassed ?? false
    };

    if (initialGain === 0) {
      trackObj.muteBtn.classList.add('active');
    }

    updateEqBtnState(trackObj);
    trackRegistry.set(tab.id, trackObj);

    // ACT ボタンのトグル動作（ON ⇔ OFF / バイパス）
    trackObj.connBtn.addEventListener('click', async () => {
      if (trackObj.bypassed) {
        // 現在 OFF -> ACT にする
        trackObj.bypassed = false;
        await connectOrUpdateTrack(trackObj);
      } else {
        // 現在 ACT -> OFF (バイパス) にする
        trackObj.bypassed = true;
        setOffUI(trackObj.connBtn);
        // バイパス時は原音スルーにする
        chrome.tabs.sendMessage(tab.id, {
          type: 'SET_BYPASS',
          bypassed: true
        }).catch(() => {});
        // メーター表示をクリア
        resetMeterUI(trackObj);
      }
      saveTrackSettings(trackObj);
    });

    // 起動時の自動接続（Auto Connect / Auto ACT）
    // 明示的にバイパス保存されていなければ自動で ACT 接続
    if (!initialBypassed) {
      connectOrUpdateTrack(trackObj);
    } else {
      setOffUI(trackObj.connBtn);
    }

    // [e] ボタン: モーダル起動
    trackObj.eqBtn.addEventListener('click', () => {
      openEqModal(trackObj);
    });

    // PAN 操作
    trackObj.panSlider.addEventListener('input', (e) => {
      const val = parseFloat(e.target.value);
      trackObj.pan = val;
      trackObj.panVal.textContent = formatPan(val);
      sendUpdate(tab.id, { pan: val });
      saveTrackSettings(trackObj);
    });

    trackObj.panSlider.addEventListener('dblclick', () => {
      trackObj.pan = 0;
      trackObj.panSlider.value = 0;
      trackObj.panVal.textContent = 'C';
      sendUpdate(tab.id, { pan: 0 });
      saveTrackSettings(trackObj);
    });

    // VOL 操作
    trackObj.volSlider.addEventListener('input', (e) => {
      const val = parseFloat(e.target.value);
      trackObj.channelGain = val;
      trackObj.volVal.textContent = `${Math.round(val * 100)}%`;
      if (val > 0) {
        trackObj.muteBtn.classList.remove('active');
        trackObj.lastActiveGain = val;
      } else {
        trackObj.muteBtn.classList.add('active');
      }
      applyTrackVolume(trackObj);
      saveTrackSettings(trackObj);
    });

    trackObj.volSlider.addEventListener('dblclick', () => {
      trackObj.channelGain = 1.0;
      trackObj.volSlider.value = 1.0;
      trackObj.volVal.textContent = '100%';
      trackObj.lastActiveGain = 1.0;
      trackObj.muteBtn.classList.remove('active');
      applyTrackVolume(trackObj);
      saveTrackSettings(trackObj);
    });

    // MUTE 操作
    trackObj.muteBtn.addEventListener('click', () => {
      if (trackObj.muteBtn.classList.contains('active')) {
        const restore = trackObj.lastActiveGain > 0 ? trackObj.lastActiveGain : 1.0;
        trackObj.channelGain = restore;
        trackObj.volSlider.value = restore;
        trackObj.volVal.textContent = `${Math.round(restore * 100)}%`;
        trackObj.muteBtn.classList.remove('active');
      } else {
        const cur = trackObj.channelGain;
        if (cur > 0) trackObj.lastActiveGain = cur;
        trackObj.channelGain = 0;
        trackObj.volSlider.value = 0;
        trackObj.volVal.textContent = '0%';
        trackObj.muteBtn.classList.add('active');
      }
      applyTrackVolume(trackObj);
      saveTrackSettings(trackObj);
    });

    // SOLO 操作
    trackObj.soloBtn.addEventListener('click', () => {
      const isAlreadySolo = (activeSoloTabId === tab.id);

      if (isAlreadySolo) {
        activeSoloTabId = null;
        trackObj.soloBtn.classList.remove('active');

        trackRegistry.forEach((t) => {
          t.soloBtn.classList.remove('active');
          const prevGain = preSoloVolumes.has(t.tabId) ? preSoloVolumes.get(t.tabId) : t.lastActiveGain;
          t.channelGain = prevGain;
          t.volSlider.value = prevGain;
          t.volVal.textContent = `${Math.round(prevGain * 100)}%`;
          if (prevGain === 0) {
            t.muteBtn.classList.add('active');
          } else {
            t.muteBtn.classList.remove('active');
          }
          applyTrackVolume(t);
          saveTrackSettings(t);
        });
        preSoloVolumes.clear();

      } else {
        if (activeSoloTabId === null) {
          preSoloVolumes.clear();
          trackRegistry.forEach((t) => {
            preSoloVolumes.set(t.tabId, t.channelGain);
          });
        }

        activeSoloTabId = tab.id;

        trackRegistry.forEach((t) => {
          if (t.tabId === tab.id) {
            t.soloBtn.classList.add('active');
            t.muteBtn.classList.remove('active');
            const target = t.lastActiveGain > 0 ? t.lastActiveGain : 1.0;
            t.channelGain = target;
            t.volSlider.value = target;
            t.volVal.textContent = `${Math.round(target * 100)}%`;
          } else {
            t.soloBtn.classList.remove('active');
            t.muteBtn.classList.add('active');
            t.channelGain = 0;
            t.volSlider.value = 0;
            t.volVal.textContent = '0%';
          }
          applyTrackVolume(t);
          saveTrackSettings(t);
        });
      }
    });
  }

  // トラックへの接続＆パラメータ適用ヘルパー（完了まで await 可能）
  async function connectOrUpdateTrack(trackObj) {
    trackObj.connBtn.textContent = '...';

    try {
      await chrome.scripting.executeScript({
        target: { tabId: trackObj.tabId },
        files: ['content.js']
      });
    } catch (err) {
      // 既に注入済み、または制限ページ等の場合はそのままメッセージ送信を試みる
    }

    const effectiveVol = calculateEffectiveVolume(trackObj.channelGain, trackObj.eqTrim);
    const isEqBypassed = !!trackObj.eqBypassed;

    return new Promise((resolve) => {
      let settled = false;
      const finish = (ok) => {
        if (settled) return;
        settled = true;
        setConnectedUI(trackObj.connBtn);
        trackObj.connected = ok;
        resolve(ok);
      };

      const timeoutId = setTimeout(() => finish(true), 600);

      try {
        chrome.tabs.sendMessage(trackObj.tabId, {
          type: 'ATTACH_AUDIO',
          pan: parseFloat(trackObj.panSlider.value),
          volume: effectiveVol,
          bypassed: false,
          eqHpf: isEqBypassed ? 0 : (trackObj.eqHpf || 0),
          eqHigh: isEqBypassed ? 0 : trackObj.eqHigh,
          eqHighFreq: 12000,
          eqMid: isEqBypassed ? 0 : trackObj.eqMid,
          eqMidFreq: trackObj.eqMidFreq,
          eqMidQ: trackObj.eqMidQ,
          eqLow: isEqBypassed ? 0 : trackObj.eqLow,
          eqLowFreq: trackObj.eqLowFreq
        }, (res) => {
          clearTimeout(timeoutId);
          if (chrome.runtime.lastError || (res && !res.success)) {
            finish(true);
          } else {
            finish(true);
          }
        });
      } catch (e) {
        clearTimeout(timeoutId);
        finish(true);
      }
    });
  }

  // トラック設定の保持（同一オリジン・セッション内のみ維持）
  function saveTrackSettings(trackObj) {
    const key = `tab_settings_${trackObj.tabId}`;
    chrome.storage.session.set({
      [key]: {
        tabId: trackObj.tabId,
        origin: trackObj.origin,
        pan: parseFloat(trackObj.panSlider.value),
        channelGain: trackObj.channelGain,
        bypassed: trackObj.bypassed,
        eqHpf: trackObj.eqHpf || 0,
        eqHigh: trackObj.eqHigh,
        eqHighFreq: 12000,
        eqMid: trackObj.eqMid,
        eqMidFreq: trackObj.eqMidFreq,
        eqMidQ: trackObj.eqMidQ,
        eqLow: trackObj.eqLow,
        eqLowFreq: trackObj.eqLowFreq,
        eqTrim: trackObj.eqTrim,
        eqBypassed: trackObj.eqBypassed
      }
    });
  }

  // --- 聖徳太子（オートパン ＆ オートレベラー）ロジック ---
  if (shotokuBtn) {
    shotokuBtn.addEventListener('click', async () => {
      // ソロ状態が有効な場合は解除して全トラックを対象にする
      if (activeSoloTabId !== null) {
        activeSoloTabId = null;
        trackRegistry.forEach((t) => {
          t.soloBtn.classList.remove('active');
        });
        preSoloVolumes.clear();
      }

      // 聖徳太子発動時は、OFFや未接続のトラックも含め全トラックを確実に ACT 化して接続完了を待つ
      const allTracks = Array.from(trackRegistry.values());
      await Promise.all(allTracks.map(async (track) => {
        if (track.bypassed || !track.connected) {
          track.bypassed = false;
          await connectOrUpdateTrack(track);
          saveTrackSettings(track);
        }
      }));

      const activeTracks = allTracks.filter(t => !t.bypassed);

      if (activeTracks.length === 0) {
        shotokuBtn.classList.add('warn');
        shotokuBtn.title = '音声シグナルが見つかりません';
        setTimeout(() => {
          shotokuBtn.classList.remove('warn');
          shotokuBtn.title = '聖徳太子（オートパン＆オートレベラー）';
        }, 1800);
        return;
      }

      shotokuBtn.classList.add('measuring');
      shotokuBtn.title = '聴取中 (2.4s)... 音声解析中';

      // バックグラウンドタブの setInterval スロットリング(1秒制限)を回避するため、
      // フォアグラウンドの popup 側から 60ms 間隔で能動サンプリングを実施
      const sampleMap = new Map();
      const fallbackAvgMsMap = new Map();
      activeTracks.forEach(t => sampleMap.set(t.tabId, []));

      const durationMs = 2400;
      const pollIntervalMs = 60;
      const startTime = performance.now();

      while (performance.now() - startTime < durationMs) {
        const tickStart = performance.now();
        await Promise.all(activeTracks.map(async (t) => {
          const res = await requestTabMeterData(t.tabId, 200);
          if (!res) return;

          if (typeof res.rawMs === 'number' && res.rawMs > 2.5e-7) {
            sampleMap.get(t.tabId).push(res.rawMs);
          }
          if (typeof res.avgRawRms === 'number' && res.avgRawRms > 0.0005) {
            fallbackAvgMsMap.set(t.tabId, res.avgRawRms * res.avgRawRms);
          }
        }));

        const elapsedTick = performance.now() - tickStart;
        if (elapsedTick < pollIntervalMs) {
          await new Promise(r => setTimeout(r, pollIntervalMs - elapsedTick));
        }
      }

      // 各トラックのゲーティング済み Post-EQ・Pre-Fader ラウドネス(RMS)を算出
      const results = activeTracks.map(t => {
        const samples = sampleMap.get(t.tabId) || [];
        let gatedRms = 0;

        if (samples.length > 0) {
          // ITU-R BS.1770 準拠: 無音除去後の平均エネルギーに対し -10dB の相対ゲートを適用し、
          // トーク配信の語間ポーズに引っ張られず実際の声のラウドネスを抽出
          const ungatedMs = samples.reduce((a, b) => a + b, 0) / samples.length;
          const relThreshold = ungatedMs * 0.1;
          const gatedSamples = samples.filter(ms => ms >= relThreshold);
          const finalMs = gatedSamples.length > 0
            ? gatedSamples.reduce((a, b) => a + b, 0) / gatedSamples.length
            : ungatedMs;
          gatedRms = Math.sqrt(finalMs);
        } else if (fallbackAvgMsMap.has(t.tabId)) {
          gatedRms = Math.sqrt(fallbackAvgMsMap.get(t.tabId));
        }

        // Preamp Trim (eqTrim: dB) を加味したフェーダー直前の実効RMS
        const trimMult = Math.pow(10, (t.eqTrim || 0) / 20);
        const preFaderRms = gatedRms * trimMult;

        return {
          tabId: t.tabId,
          rawRms: gatedRms,
          preFaderRms,
          valid: preFaderRms > 0.0005
        };
      });

      const validResults = results.filter(r => r.valid);

      if (validResults.length > 0) {
        const MIN_GAIN = 0.16; // 16% (大音量配信の十分な減衰を許容)
        const MAX_GAIN = 1.38; // 138% (フェーダー上限 140% 手前の安全上限)

        // マスター合成時に快適スイートスポット(-16.5〜-13.0 LUFS、中心 -14.8 LUFS)へ収まる1トラックあたりの理想RMS
        const K = validResults.length;
        const perTrackSweetLufs = -14.8 - 10 * Math.log10(K) * 0.55;
        const sweetSpotRms = lufsToRms(perTrackSweetLufs);

        // 全有効トラックの対数平均(幾何平均)RMS
        const logSum = validResults.reduce((sum, r) => sum + Math.log(r.preFaderRms), 0);
        const geoMeanRms = Math.exp(logSum / K);

        // 1窓時はスイートスポットへ、複数窓時は現状の対数平均とスイートスポットをブレンド
        let targetRms = (K === 1)
          ? sweetSpotRms
          : Math.sqrt(geoMeanRms * sweetSpotRms);

        // スイートスポット範囲 (-19.0 LUFS 〜 -14.0 LUFS) に目標値をクランプ
        targetRms = Math.max(lufsToRms(-19.0), Math.min(lufsToRms(-14.0), targetRms));

        // 最も小さい配信を MAX_GAIN(138%) までブーストした際の到達可能RMSを上限とすることで、
        // 小さい配信が頭打ちになっても大きい配信側を確実に下げて全トラックの音量を揃える
        const minPreFaderRms = Math.min(...validResults.map(r => r.preFaderRms));
        const maxPreFaderRms = Math.max(...validResults.map(r => r.preFaderRms));
        const maxReachableForQuietest = minPreFaderRms * MAX_GAIN;
        const minReachableForLoudest = maxPreFaderRms * MIN_GAIN;

        if (maxReachableForQuietest >= minReachableForLoudest) {
          targetRms = Math.min(targetRms, maxReachableForQuietest);
          targetRms = Math.max(targetRms, minReachableForLoudest);
        } else {
          // 音量差が極めて大きい場合は中間点へ寄せる
          targetRms = Math.sqrt(maxReachableForQuietest * minReachableForLoudest);
        }

        results.forEach(res => {
          const track = trackRegistry.get(res.tabId);
          if (!track || !res.valid) return;

          // preFaderRms はフェーダー前の値なので、旧 channelGain を掛けず直接目標ゲインを算出
          let newGain = targetRms / res.preFaderRms;
          newGain = Math.min(MAX_GAIN, Math.max(MIN_GAIN, newGain));
          newGain = Math.round(newGain * 50) / 50; // 0.02 ステップに丸め

          track.channelGain = newGain;
          track.lastActiveGain = newGain;
          track.volSlider.value = newGain;
          track.volVal.textContent = `${Math.round(newGain * 100)}%`;
          track.muteBtn.classList.remove('active');
          applyTrackVolume(track);
          saveTrackSettings(track);
        });
      }

      const N = activeTracks.length;
      let maxPan = 0;
      if (N === 1) {
        maxPan = 0;
      } else if (N >= 2 && N <= 4) {
        maxPan = 0.85;
      } else {
        maxPan = 1.00;
      }

      activeTracks.forEach((track, index) => {
        let targetPan = 0;
        if (N === 1) {
          targetPan = 0;
        } else {
          targetPan = -maxPan + (2 * maxPan / (N - 1)) * index;
        }
        targetPan = Math.round(targetPan * 100) / 100;

        track.pan = targetPan;
        track.panSlider.value = targetPan;
        track.panVal.textContent = formatPan(targetPan);
        sendUpdate(track.tabId, { pan: targetPan });
        saveTrackSettings(track);
      });

      shotokuBtn.classList.remove('measuring');
      shotokuBtn.classList.add('done');
      shotokuBtn.title = '整音完了！音量と定位を最適化しました';

      setTimeout(() => {
        shotokuBtn.classList.remove('done');
        shotokuBtn.title = '聖徳太子（オートパン＆オートレベラー）';
      }, 1800);
    });
  }

  // --- リアルタイム・メータリング監視（平均LUFS ＆ 縦型LEDレベルメーター） ---
  let meterInterval = null;
  let isPollingMeters = false;

  function startMetering() {
    if (meterInterval) clearInterval(meterInterval);

    meterInterval = setInterval(async () => {
      if (isPollingMeters) return;
      isPollingMeters = true;

      try {
        const activeTracks = Array.from(trackRegistry.values()).filter(t => !t.bypassed);

        if (activeTracks.length === 0) {
          if (masterMeterBar) masterMeterBar.style.height = '0%';
          if (masterLufsNum) {
            masterLufsNum.textContent = '-∞';
            masterLufsNum.className = 'lufs-num';
          }
          return;
        }

        let maxChannelPeak = 0;
        let sumChannelMs = 0;
        let validCount = 0;

        await Promise.all(activeTracks.map(async (t) => {
          const res = await requestTabMeterData(t.tabId, 200);
          if (!res) return;

          if (res.bypassed) {
            resetMeterUI(t);
            return;
          }

          // 各チャンネルのメーターは Channel Gain × Preamp Trim を反映（マスター音量とは独立）
          const trimMult = Math.pow(10, (t.eqTrim || 0) / 20);
          const chGain = Math.max(0, t.channelGain * trimMult);

          let chLufs = -70;
          let chPeak = 0;

          if (typeof res.avgRawRms === 'number') {
            const chMs = (res.avgRawRms * res.avgRawRms) * (chGain * chGain);
            if (chGain > 0.0001 && chMs > 1e-9) {
              chLufs = Math.max(-70, Math.round((10 * Math.log10(chMs) - 0.691) * 10) / 10);
              sumChannelMs += chMs;
              validCount++;
            }
            const rawPeak = typeof res.rawPeak === 'number' ? res.rawPeak : (res.peak || 0);
            chPeak = Math.min(1.0, rawPeak * chGain);
          } else {
            chLufs = res.lufs;
            chPeak = res.peak;
            if (chLufs > -60) {
              sumChannelMs += Math.pow(10, (chLufs + 0.691) / 10);
              validCount++;
            }
          }

          if (chPeak > maxChannelPeak) {
            maxChannelPeak = chPeak;
          }

          updateTrackMeterUI(t, chLufs, chPeak);
        }));

        // マスタートラック（STEREO OUT）のメーター反映（マスター音量を1回だけ正確に適用）
        const effMaster = isMasterMuted ? 0 : masterVolume;
        const finalMasterPeak = Math.min(1.0, maxChannelPeak * effMaster);

        if (masterMeterBar) {
          masterMeterBar.style.height = `${Math.round(finalMasterPeak * 100)}%`;
        }
        if (masterLufsNum) {
          const masterMs = sumChannelMs * (effMaster * effMaster);
          if (isMasterMuted || validCount === 0 || effMaster <= 0.0001 || masterMs <= 1e-9) {
            masterLufsNum.textContent = '-∞';
            masterLufsNum.className = 'lufs-num';
          } else {
            const masterLufs = 10 * Math.log10(masterMs) - 0.691;
            setLufsDisplay(masterLufsNum, masterLufs);
          }
        }
      } finally {
        isPollingMeters = false;
      }
    }, 120);
  }


  startMetering();

  // --- 各ノブのコントローラー生成 (Arturia Pre 1973 風 独立ノブ仕様) ---
  const hpfSteps = [0, 50, 80, 160, 300];

  // 1. 赤いマルコーニノブ (Input Trim: -6dB〜+6dB)
  setupRotaryControl({
    element: mRedKnob,
    getValue: () => currentEditingTrack ? (currentEditingTrack.eqTrim || 0) : 0,
    setValue: (val) => {
      if (currentEditingTrack) currentEditingTrack.eqTrim = val;
    },
    min: -6.0,
    max: 6.0,
    step: 0.5,
    defaultValue: 0.0,
    sensitivity: 1.0,
    applyTransform: (el, angle) => { el.style.transform = `rotate(${angle}deg)`; },
    onUpdate: (val) => {
      if (!currentEditingTrack) return;
      mRedVal.textContent = formatGain(val);
      applyTrackVolume(currentEditingTrack);
      updateEqBtnState(currentEditingTrack);
      saveTrackSettings(currentEditingTrack);
    }
  });

  // 2. HIGH PASS FILTER (HPF: OFF, 50, 80, 160, 300 Hz)
  setupRotaryControl({
    element: mHpfKnob,
    getValue: () => {
      if (!currentEditingTrack) return 0;
      const idx = hpfSteps.indexOf(currentEditingTrack.eqHpf || 0);
      return idx >= 0 ? idx : 0;
    },
    setValue: (idx) => {
      if (currentEditingTrack) {
        const rounded = Math.round(idx);
        currentEditingTrack.eqHpf = hpfSteps[rounded] !== undefined ? hpfSteps[rounded] : 0;
      }
    },
    min: 0,
    max: 4,
    step: 1,
    defaultValue: 0,
    angleMin: -120,
    angleMax: 120,
    sensitivity: 1.0,
    applyTransform: (el, angle) => { el.style.transform = `rotate(${angle}deg)`; },
    onUpdate: (idx) => {
      if (!currentEditingTrack) return;
      const val = hpfSteps[Math.round(idx)] || 0;
      mEqHpfVal.textContent = formatHpf(val);
      syncEditingTrackEq();
    }
  });

  // 3. LOW バンド: GAIN ノブ (青ノブ: -12dB〜+12dB)
  setupRotaryControl({
    element: mLowGainKnob,
    getValue: () => currentEditingTrack ? currentEditingTrack.eqLow : 0,
    setValue: (val) => {
      if (currentEditingTrack) currentEditingTrack.eqLow = val;
    },
    min: -12.0,
    max: 12.0,
    step: 0.5,
    defaultValue: 0.0,
    sensitivity: 1.0,
    applyTransform: (el, angle) => { el.style.transform = `rotate(${angle}deg)`; },
    onUpdate: (val) => {
      if (!currentEditingTrack) return;
      mLowGainVal.textContent = formatGain(val);
      syncEditingTrackEq();
    }
  });

  // 4. LOW バンド: FREQ ノブ (シルバーノブ: 40Hz〜600Hz)
  setupRotaryControl({
    element: mLowFreqKnob,
    getValue: () => currentEditingTrack ? currentEditingTrack.eqLowFreq : 200,
    setValue: (val) => {
      if (currentEditingTrack) currentEditingTrack.eqLowFreq = val;
    },
    min: 40,
    max: 600,
    step: 10,
    defaultValue: 200,
    isLog: true,
    sensitivity: 1.0,
    applyTransform: (el, angle) => { el.style.transform = `rotate(${angle}deg)`; },
    onUpdate: (val) => {
      if (!currentEditingTrack) return;
      mLowFreqVal.textContent = formatFreq(val);
      syncEditingTrackEq();
    }
  });

  // 5. MID バンド: GAIN ノブ (青ノブ: -12dB〜+12dB)
  setupRotaryControl({
    element: mMidGainKnob,
    getValue: () => currentEditingTrack ? currentEditingTrack.eqMid : 0,
    setValue: (val) => {
      if (currentEditingTrack) currentEditingTrack.eqMid = val;
    },
    min: -12.0,
    max: 12.0,
    step: 0.5,
    defaultValue: 0.0,
    sensitivity: 1.0,
    applyTransform: (el, angle) => { el.style.transform = `rotate(${angle}deg)`; },
    onUpdate: (val) => {
      if (!currentEditingTrack) return;
      mMidGainVal.textContent = formatGain(val);
      syncEditingTrackEq();
    }
  });

  // 6. MID バンド: FREQ ノブ (シルバーノブ: 200Hz〜6kHz)
  setupRotaryControl({
    element: mMidFreqKnob,
    getValue: () => currentEditingTrack ? currentEditingTrack.eqMidFreq : 1500,
    setValue: (val) => {
      if (currentEditingTrack) currentEditingTrack.eqMidFreq = val;
    },
    min: 200,
    max: 6000,
    step: 50,
    defaultValue: 1500,
    isLog: true,
    sensitivity: 1.0,
    applyTransform: (el, angle) => { el.style.transform = `rotate(${angle}deg)`; },
    onUpdate: (val) => {
      if (!currentEditingTrack) return;
      mMidFreqVal.textContent = formatFreq(val);
      syncEditingTrackEq();
    }
  });

  // 7. MID バンド: Q コントロール (ミニノブ: 0.3〜5.0)
  setupRotaryControl({
    element: mMidQKnob,
    getValue: () => currentEditingTrack ? currentEditingTrack.eqMidQ : 1.0,
    setValue: (val) => {
      if (currentEditingTrack) currentEditingTrack.eqMidQ = val;
    },
    min: 0.3,
    max: 5.0,
    step: 0.1,
    defaultValue: 1.0,
    sensitivity: 1.0,
    applyTransform: (el, angle) => { el.style.transform = `rotate(${angle}deg)`; },
    onUpdate: (val) => {
      if (!currentEditingTrack) return;
      mMidQVal.textContent = formatQ(val);
      syncEditingTrackEq();
    }
  });

  // 8. HIGH バンド: GAIN ノブ (青ノブ: -12dB〜+12dB / 12kHz FIXED)
  setupRotaryControl({
    element: mHiGainKnob,
    getValue: () => currentEditingTrack ? currentEditingTrack.eqHigh : 0,
    setValue: (val) => {
      if (currentEditingTrack) currentEditingTrack.eqHigh = val;
    },
    min: -12.0,
    max: 12.0,
    step: 0.5,
    defaultValue: 0.0,
    sensitivity: 1.0,
    applyTransform: (el, angle) => { el.style.transform = `rotate(${angle}deg)`; },
    onUpdate: (val) => {
      if (!currentEditingTrack) return;
      mHiGainVal.textContent = formatGain(val);
      syncEditingTrackEq();
    }
  });

  // EQ パラメータの同期＆Web Audio送信ヘルパー
  function syncEditingTrackEq() {
    if (!currentEditingTrack) return;
    const isBypassed = !!currentEditingTrack.eqBypassed;
    sendUpdate(currentEditingTrack.tabId, {
      eqHpf: isBypassed ? 0 : (currentEditingTrack.eqHpf || 0),
      eqHigh: isBypassed ? 0 : currentEditingTrack.eqHigh,
      eqHighFreq: 12000,
      eqMid: isBypassed ? 0 : currentEditingTrack.eqMid,
      eqMidFreq: currentEditingTrack.eqMidFreq,
      eqMidQ: currentEditingTrack.eqMidQ,
      eqLow: isBypassed ? 0 : currentEditingTrack.eqLow,
      eqLowFreq: currentEditingTrack.eqLowFreq
    });
    updateEqBtnState(currentEditingTrack);
    saveTrackSettings(currentEditingTrack);
  }

  // EQ ACTIVE / BYPASS トグルスイッチ動作
  if (mEqToggle) {
    mEqToggle.addEventListener('click', () => {
      if (!currentEditingTrack) return;
      currentEditingTrack.eqBypassed = !currentEditingTrack.eqBypassed;
      updateEqToggleUI(currentEditingTrack);
      syncEditingTrackEq();
    });
  }

  function updateEqToggleUI(trackObj) {
    if (!trackObj) return;
    if (trackObj.eqBypassed) {
      if (mEqToggle) mEqToggle.classList.add('off');
      if (eqLampLed) eqLampLed.classList.add('bypass');
    } else {
      if (mEqToggle) mEqToggle.classList.remove('off');
      if (eqLampLed) eqLampLed.classList.remove('bypass');
    }
  }

  // --- EQ Modal のバインドと開閉 ---
  function openEqModal(trackObj) {
    currentEditingTrack = trackObj;
    modalTrackTitle.textContent = `${trackObj.title}`;

    mRedVal.textContent = formatGain(trackObj.eqTrim || 0);
    mEqHpfVal.textContent = formatHpf(trackObj.eqHpf || 0);
    mLowGainVal.textContent = formatGain(trackObj.eqLow);
    mLowFreqVal.textContent = formatFreq(trackObj.eqLowFreq);
    mMidGainVal.textContent = formatGain(trackObj.eqMid);
    mMidFreqVal.textContent = formatFreq(trackObj.eqMidFreq);
    mMidQVal.textContent = formatQ(trackObj.eqMidQ);
    mHiGainVal.textContent = formatGain(trackObj.eqHigh);

    updateEqToggleUI(trackObj);

    // 全ノブを現在のトラック角度に描画
    rotaryControls.forEach(ctrl => ctrl.refresh());

    eqModal.style.display = 'flex';
  }

  function closeEqModal() {
    eqModal.style.display = 'none';
    currentEditingTrack = null;
  }

  modalCloseBtn.addEventListener('click', closeEqModal);
  modalDoneBtn.addEventListener('click', closeEqModal);

  // FLAT EQ (RESET) ボタン
  modalResetBtn.addEventListener('click', () => {
    if (!currentEditingTrack) return;
    currentEditingTrack.eqHigh = 0;
    currentEditingTrack.eqMid = 0;
    currentEditingTrack.eqLow = 0;
    currentEditingTrack.eqHpf = 0;
    currentEditingTrack.eqTrim = 0;

    mRedVal.textContent = formatGain(0);
    mEqHpfVal.textContent = formatHpf(0);
    mLowGainVal.textContent = formatGain(0);
    mMidGainVal.textContent = formatGain(0);
    mHiGainVal.textContent = formatGain(0);

    rotaryControls.forEach(ctrl => ctrl.refresh());

    applyTrackVolume(currentEditingTrack);
    syncEditingTrackEq();
  });

  function updateEqBtnState(trackObj) {
    const isCustomized = (
      trackObj.eqHigh !== 0 ||
      trackObj.eqMid !== 0 ||
      trackObj.eqLow !== 0 ||
      (trackObj.eqHpf && trackObj.eqHpf !== 0) ||
      (trackObj.eqTrim && trackObj.eqTrim !== 0)
    );
    if (isCustomized && !trackObj.eqBypassed) {
      trackObj.eqBtn.classList.add('active-eq');
    } else {
      trackObj.eqBtn.classList.remove('active-eq');
    }
  }


  masterVolSlider.addEventListener('input', (e) => {
    masterVolume = parseFloat(e.target.value);
    masterVolVal.textContent = `${Math.round(masterVolume * 100)}%`;
    if (masterVolume > 0 && isMasterMuted) {
      isMasterMuted = false;
      masterMuteBtn.classList.remove('active');
    }
    lastActiveMasterVolume = masterVolume > 0 ? masterVolume : 1.0;
    saveMasterState();
    syncAllTracks();
  });

  masterVolSlider.addEventListener('dblclick', () => {
    masterVolume = 1.0;
    masterVolSlider.value = 1.0;
    masterVolVal.textContent = '100%';
    lastActiveMasterVolume = 1.0;
    isMasterMuted = false;
    masterMuteBtn.classList.remove('active');
    saveMasterState();
    syncAllTracks();
  });

  masterMuteBtn.addEventListener('click', () => {
    isMasterMuted = !isMasterMuted;
    if (isMasterMuted) {
      masterMuteBtn.classList.add('active');
    } else {
      masterMuteBtn.classList.remove('active');
    }
    saveMasterState();
    syncAllTracks();
  });

  function calculateEffectiveVolume(gain, trim = 0) {
    if (isMasterMuted) return 0;
    const trimMult = Math.pow(10, (trim || 0) / 20);
    return gain * masterVolume * trimMult;
  }

  function applyTrackVolume(trackObj) {
    const eff = calculateEffectiveVolume(trackObj.channelGain, trackObj.eqTrim);
    sendUpdate(trackObj.tabId, { volume: eff });
  }

  function syncAllTracks() {
    trackRegistry.forEach((t) => {
      applyTrackVolume(t);
    });
  }

  function saveMasterState() {
    chrome.storage.local.set({
      masterVol: masterVolume,
      masterMuted: isMasterMuted
    });
  }

});
