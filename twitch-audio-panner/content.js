window.__audioPannerSession = window.__audioPannerSession || {
  connected: false,
  bypassed: false,
  audioCtx: null,
  source: null,
  analyser: null,
  eqHpf: null,
  eqLow: null,
  eqMid: null,
  eqHigh: null,
  gain: null,
  panner: null,
  currentMedia: null,
  pan: 0,
  volume: 1.0,
  eqHpfFreq: 0, // 0 = OFF, 50, 80, 160, 300
  eqLowGain: 0,
  eqLowFreq: 200,
  eqMidGain: 0,
  eqMidFreq: 1500,
  eqMidQ: 1.0,
  eqHighGain: 0,
  eqHighFreq: 12000,
  msHistory: [], // 直近のPost-EQ・Pre-Gain平均二乗エネルギー(Mean Square)履歴
  observer: null,
  playListenerBound: false
};

(function() {
  const session = window.__audioPannerSession;

  // 音声パラメータを Web Audio ノードへ反映（バイパス考慮）
  function applyParams() {
    if (!session.connected || !session.audioCtx) return;
    if (session.audioCtx.state === 'suspended') {
      session.audioCtx.resume().catch(() => {});
    }
    const now = session.audioCtx.currentTime;

    if (session.bypassed) {
      // バイパス時: 原音スルー（音量 100%, PAN C, EQ Flat）
      if (session.gain) session.gain.gain.setTargetAtTime(1.0, now, 0.015);
      if (session.panner) session.panner.pan.setTargetAtTime(0, now, 0.015);
      if (session.eqHpf) session.eqHpf.frequency.setTargetAtTime(10, now, 0.015);
      if (session.eqLow) session.eqLow.gain.setTargetAtTime(0, now, 0.015);
      if (session.eqMid) session.eqMid.gain.setTargetAtTime(0, now, 0.015);
      if (session.eqHigh) session.eqHigh.gain.setTargetAtTime(0, now, 0.015);
    } else {
      // ACT時: ユーザー設定値を滑らかに反映（クリックノイズ防止）
      const targetVol = typeof session.volume === 'number' ? Math.max(0, session.volume) : 1.0;
      const targetPan = typeof session.pan === 'number' ? Math.max(-1, Math.min(1, session.pan)) : 0;
      if (session.gain) session.gain.gain.setTargetAtTime(targetVol, now, 0.015);
      if (session.panner) session.panner.pan.setTargetAtTime(targetPan, now, 0.015);
      if (session.eqHpf) {
        const hpfVal = session.eqHpfFreq > 0 ? session.eqHpfFreq : 10;
        session.eqHpf.frequency.setTargetAtTime(hpfVal, now, 0.015);
      }
      if (session.eqLow) {
        session.eqLow.gain.setTargetAtTime(session.eqLowGain, now, 0.015);
        session.eqLow.frequency.setTargetAtTime(session.eqLowFreq, now, 0.015);
      }
      if (session.eqMid) {
        session.eqMid.gain.setTargetAtTime(session.eqMidGain, now, 0.015);
        session.eqMid.frequency.setTargetAtTime(session.eqMidFreq, now, 0.015);
        session.eqMid.Q.setTargetAtTime(session.eqMidQ, now, 0.015);
      }
      if (session.eqHigh) {
        session.eqHigh.gain.setTargetAtTime(session.eqHighGain, now, 0.015);
        session.eqHigh.frequency.setTargetAtTime(session.eqHighFreq, now, 0.015);
      }
    }
  }

  // ページ内で最も適切な（実際に再生中・音声出力中の）メディア要素を選択
  function findBestMediaElement() {
    const mediaList = Array.from(document.querySelectorAll('video, audio'));
    if (mediaList.length === 0) return null;

    // 1. 再生中かつミュートされておらず、実際に進行している要素を最優先
    const playingAudible = mediaList.find(
      m => !m.paused && !m.ended && !m.muted && m.volume > 0 && m.readyState >= 2
    );
    if (playingAudible) return playingAudible;

    // 2. 再生中の要素
    const playing = mediaList.find(m => !m.paused && !m.ended);
    if (playing) return playing;

    // 3. 既に接続済みの要素がまだDOM内に存在する場合は維持
    if (session.currentMedia && document.contains(session.currentMedia)) {
      return session.currentMedia;
    }

    // 4. ソースが設定されている要素
    const withSrc = mediaList.find(m => m.currentSrc || m.src || m.srcObject || m.readyState > 0);
    if (withSrc) return withSrc;

    return mediaList[0];
  }

  // メディア要素を Web Audio グラフへ接続
  function hookMediaElement(media) {
    if (!media) return false;
    if (session.currentMedia === media && session.connected && session.audioCtx) {
      if (session.audioCtx.state === 'suspended') {
        session.audioCtx.resume().catch(() => {});
      }
      applyParams();
      return true;
    }

    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!session.audioCtx) {
        session.audioCtx = new AudioContext();
      }
      if (session.audioCtx.state === 'suspended') {
        session.audioCtx.resume().catch(() => {});
      }

      // ノード群の作成（未作成の場合）
      if (!session.analyser) {
        session.analyser = session.audioCtx.createAnalyser();
        session.analyser.fftSize = 2048;
        session.analyser.smoothingTimeConstant = 0.2;
      }

      if (!session.eqHpf) {
        session.eqHpf = session.audioCtx.createBiquadFilter();
        session.eqHpf.type = 'highpass';
        session.eqHpf.Q.value = 0.707;
      }
      if (!session.eqLow) {
        session.eqLow = session.audioCtx.createBiquadFilter();
        session.eqLow.type = 'lowshelf';
      }
      if (!session.eqMid) {
        session.eqMid = session.audioCtx.createBiquadFilter();
        session.eqMid.type = 'peaking';
      }
      if (!session.eqHigh) {
        session.eqHigh = session.audioCtx.createBiquadFilter();
        session.eqHigh.type = 'highshelf';
      }
      if (!session.gain) {
        session.gain = session.audioCtx.createGain();
      }
      if (!session.panner) {
        session.panner = session.audioCtx.createStereoPanner();
      }

      // 既存の別メディアの source があれば、エフェクトチェーンから外して直接 destination へ逃がす（無音化防止）
      if (session.source && session.currentMedia && session.currentMedia !== media) {
        try {
          session.source.disconnect();
          session.source.connect(session.audioCtx.destination);
        } catch (e) {}
      }

      // HTMLMediaElement に対する createMediaElementSource は1度のみ呼べる
      if (!media.__audioPannerSource) {
        media.__audioPannerSource = session.audioCtx.createMediaElementSource(media);
      } else {
        try { media.__audioPannerSource.disconnect(); } catch (e) {}
      }
      session.source = media.__audioPannerSource;
      session.currentMedia = media;

      // ルーティング接続:
      // Source -> HPF -> LOW -> MID -> HIGH -> Gain -> Panner -> Destination
      //                                     └-> Analyser (Post-EQ / Pre-Gain)
      try {
        session.eqHpf.disconnect();
        session.eqLow.disconnect();
        session.eqMid.disconnect();
        session.eqHigh.disconnect();
        session.gain.disconnect();
        session.panner.disconnect();
      } catch (e) {}

      session.source.connect(session.eqHpf);
      session.eqHpf.connect(session.eqLow);
      session.eqLow.connect(session.eqMid);
      session.eqMid.connect(session.eqHigh);

      // Post-EQ / Pre-Gain 位置に AnalyserNode を接続（EQ・HPFの変化をラウドネス計測へ正確に反映）
      session.eqHigh.connect(session.analyser);

      // フェーダー(Gain) & 定位(Panner)
      session.eqHigh.connect(session.gain);
      session.gain.connect(session.panner);
      session.panner.connect(session.audioCtx.destination);

      session.connected = true;
      applyParams();

      // メディア再生開始イベントでも念のため AudioContext を resume
      if (!media.__audioPannerPlayBound) {
        media.__audioPannerPlayBound = true;
        media.addEventListener('play', () => {
          if (session.audioCtx && session.audioCtx.state === 'suspended') {
            session.audioCtx.resume().catch(() => {});
          }
          if (session.currentMedia !== media) {
            hookMediaElement(media);
          }
        }, { passive: true });
      }

      return true;
    } catch (err) {
      console.warn('Audio hook error:', err);
      return false;
    }
  }

  // 現在フックしているメディアが停止中・DOM外の場合にアクティブなメディアへ再フックを試みる
  function ensureActiveMedia() {
    if (!session.currentMedia || !document.contains(session.currentMedia) || session.currentMedia.paused) {
      const best = findBestMediaElement();
      if (best && best !== session.currentMedia) {
        hookMediaElement(best);
      }
    }
    if (session.audioCtx && session.audioCtx.state === 'suspended') {
      session.audioCtx.resume().catch(() => {});
    }
  }

  // DOM 内の media 要素を監視（SPA・YouTube/Twitch動画遷移対応）
  function setupMediaWatcher() {
    if (!session.playListenerBound) {
      session.playListenerBound = true;
      document.addEventListener('play', (e) => {
        const target = e.target;
        if (target && (target.tagName === 'VIDEO' || target.tagName === 'AUDIO')) {
          hookMediaElement(target);
        }
      }, true);
    }

    if (session.observer) return;

    session.observer = new MutationObserver(() => {
      const best = findBestMediaElement();
      if (best && best !== session.currentMedia) {
        if (!session.currentMedia || session.currentMedia.paused || !document.contains(session.currentMedia)) {
          hookMediaElement(best);
        }
      }
    });

    session.observer.observe(document.documentElement || document.body, {
      childList: true,
      subtree: true
    });
  }

  // 1フレーム分の波形解析（Post-EQ / Pre-Gain の rawMs, rawRms, rawPeak を算出し履歴を更新）
  function sampleCurrentFrame() {
    ensureActiveMedia();
    if (!session || !session.connected || !session.analyser) {
      return null;
    }

    const fftSize = session.analyser.fftSize;
    if (!session._timeDomainBuf || session._timeDomainBuf.length !== fftSize) {
      session._timeDomainBuf = new Float32Array(fftSize);
    }
    const timeDomainData = session._timeDomainBuf;
    session.analyser.getFloatTimeDomainData(timeDomainData);

    let maxVal = 0;
    let sumSquares = 0;
    for (let i = 0; i < fftSize; i++) {
      const val = timeDomainData[i];
      const abs = val < 0 ? -val : val;
      if (abs > maxVal) maxVal = abs;
      sumSquares += val * val;
    }

    const rawMs = sumSquares / fftSize;
    const rawRms = Math.sqrt(rawMs);

    // 履歴バッファには Post-EQ / Pre-Gain の平均二乗エネルギー(rawMs)を保存
    // （フェーダー変更時に過去の音量履歴が干渉せず即座にLUFS表示へ反映されるようにするため）
    session.msHistory = session.msHistory || [];
    if (rawMs > 1e-7) {
      // 約 -70 dBFS 以上の有効フレームを蓄積（最大25フレーム ≒ 約3秒のShort-Term窓）
      session.msHistory.push(rawMs);
      if (session.msHistory.length > 25) {
        session.msHistory.shift();
      }
    } else {
      // 無音時は緩やかに減衰
      if (session.msHistory.length > 0) {
        session.msHistory.shift();
      }
      if (session.msHistory.length > 0) {
        session.msHistory.shift();
      }
    }

    // 語間の微小な無音に引っ張られないよう、ITU-R BS.1770 相対ゲート(-10dB)相当のエネルギー平均を算出
    let avgRawMs = rawMs;
    if (session.msHistory.length > 0) {
      const ungatedMs = session.msHistory.reduce((a, b) => a + b, 0) / session.msHistory.length;
      const relGate = ungatedMs * 0.1; // -10 dB relative gate
      const gated = session.msHistory.filter(ms => ms >= relGate);
      avgRawMs = gated.length > 0
        ? gated.reduce((a, b) => a + b, 0) / gated.length
        : ungatedMs;
    }

    return {
      rawMs,
      rawRms,
      rawPeak: maxVal,
      avgRawMs,
      avgRawRms: Math.sqrt(avgRawMs)
    };
  }

  // メッセージハンドラ
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    // 1. 状態取得
    if (message.type === 'GET_STATUS') {
      sendResponse({
        connected: session.connected,
        bypassed: session.bypassed,
        pan: session.pan,
        volume: session.volume,
        eqHpf: session.eqHpfFreq,
        eqLow: session.eqLowGain,
        eqLowFreq: session.eqLowFreq,
        eqMid: session.eqMidGain,
        eqMidFreq: session.eqMidFreq,
        eqMidQ: session.eqMidQ,
        eqHigh: session.eqHighGain,
        eqHighFreq: session.eqHighFreq
      });
      return true;
    }

    // 2. 音声接続
    if (message.type === 'ATTACH_AUDIO') {
      if (typeof message.pan === 'number') session.pan = message.pan;
      if (typeof message.volume === 'number') session.volume = message.volume;
      if (typeof message.eqHpf === 'number') session.eqHpfFreq = message.eqHpf;
      if (typeof message.eqLow === 'number') session.eqLowGain = message.eqLow;
      if (typeof message.eqLowFreq === 'number') session.eqLowFreq = message.eqLowFreq;
      if (typeof message.eqMid === 'number') session.eqMidGain = message.eqMid;
      if (typeof message.eqMidFreq === 'number') session.eqMidFreq = message.eqMidFreq;
      if (typeof message.eqMidQ === 'number') session.eqMidQ = message.eqMidQ;
      if (typeof message.eqHigh === 'number') session.eqHighGain = message.eqHigh;
      if (typeof message.eqHighFreq === 'number') session.eqHighFreq = message.eqHighFreq;
      if (typeof message.bypassed === 'boolean') session.bypassed = message.bypassed;

      setupMediaWatcher();

      let media = findBestMediaElement();
      if (!media) {
        // メディアがまだない場合は DOM 追加を待機するが、接続準備完了として success を返す
        const waitTimer = setInterval(() => {
          media = findBestMediaElement();
          if (media) {
            clearInterval(waitTimer);
            hookMediaElement(media);
          }
        }, 300);
        setTimeout(() => clearInterval(waitTimer), 10000);

        sendResponse({ success: true, waitingForMedia: true });
        return true;
      }

      const ok = hookMediaElement(media);
      sendResponse({ success: ok });
      return true;
    }

    // 3. バイパス切替
    if (message.type === 'SET_BYPASS') {
      session.bypassed = !!message.bypassed;
      applyParams();
      sendResponse({ success: true, bypassed: session.bypassed });
      return true;
    }

    // 4. パラメータ更新
    if (message.type === 'UPDATE_PARAMS') {
      if (typeof message.pan === 'number') session.pan = message.pan;
      if (typeof message.volume === 'number') session.volume = message.volume;
      if (typeof message.eqHpf === 'number') session.eqHpfFreq = message.eqHpf;
      if (typeof message.eqLow === 'number') session.eqLowGain = message.eqLow;
      if (typeof message.eqLowFreq === 'number') session.eqLowFreq = message.eqLowFreq;
      if (typeof message.eqMid === 'number') session.eqMidGain = message.eqMid;
      if (typeof message.eqMidFreq === 'number') session.eqMidFreq = message.eqMidFreq;
      if (typeof message.eqMidQ === 'number') session.eqMidQ = message.eqMidQ;
      if (typeof message.eqHigh === 'number') session.eqHighGain = message.eqHigh;
      if (typeof message.eqHighFreq === 'number') session.eqHighFreq = message.eqHighFreq;
      if (typeof message.bypassed === 'boolean') session.bypassed = message.bypassed;

      applyParams();
      sendResponse({ success: true });
      return true;
    }

    // 5. 音声解析・ラウドネス計測（聖徳太子の単発フォールバック用・実時間ベース）
    if (message.type === 'MEASURE_LOUDNESS') {
      ensureActiveMedia();
      if (!session || !session.connected || !session.analyser) {
        sendResponse({ success: false, error: 'Not connected' });
        return true;
      }

      const durationMs = typeof message.durationMs === 'number' ? message.durationMs : 2200;
      const intervalMs = 50;
      const startTime = performance.now();
      const validMsSamples = [];

      const collectSample = () => {
        const frame = sampleCurrentFrame();
        if (frame && frame.rawMs > 2.5e-7) {
          validMsSamples.push(frame.rawMs);
        }
      };

      collectSample();

      const timer = setInterval(() => {
        collectSample();

        if (performance.now() - startTime >= durationMs) {
          clearInterval(timer);
          // バックグラウンドタブのタイマースロットリング等でサンプルが少ない場合は msHistory も活用
          const sourceSamples = validMsSamples.length >= 3
            ? validMsSamples
            : (session.msHistory && session.msHistory.length > 0 ? session.msHistory : validMsSamples);

          if (sourceSamples.length > 0) {
            const ungatedMs = sourceSamples.reduce((a, b) => a + b, 0) / sourceSamples.length;
            const relThreshold = ungatedMs * 0.1; // -10 dB relative gate (ITU-R BS.1770)
            const gatedSamples = sourceSamples.filter(ms => ms >= relThreshold);
            const finalMs = gatedSamples.length > 0
              ? gatedSamples.reduce((a, b) => a + b, 0) / gatedSamples.length
              : ungatedMs;
            const gatedRms = Math.sqrt(finalMs);

            sendResponse({
              success: true,
              rms: gatedRms,
              rawMs: finalMs,
              validCount: sourceSamples.length
            });
          } else {
            sendResponse({
              success: true,
              rms: 0,
              rawMs: 0,
              validCount: 0
            });
          }
        }
      }, intervalMs);

      return true;
    }

    // 6. リアルタイム・メータリング（平均LUFS ＆ LEDピークバー ＆ 聖徳太子リアルタイムサンプリング対応）
    if (message.type === 'GET_METER_DATA') {
      const frame = sampleCurrentFrame();
      if (!frame) {
        sendResponse({ success: false, error: 'Not connected' });
        return true;
      }

      // バイパス中ならメーターは表示クリア
      if (session.bypassed) {
        sendResponse({
          success: true,
          bypassed: true,
          lufs: -70,
          peak: 0,
          rawRms: frame.rawRms,
          rawMs: frame.rawMs,
          avgRawRms: frame.avgRawRms
        });
        return true;
      }

      // ミュート時 (session.volume === 0) に 1.0 へフォールバックしないよう厳密に判定
      const curVol = (typeof session.volume === 'number') ? Math.max(0, session.volume) : 1.0;
      const effectivePeak = frame.rawPeak * curVol;
      const effectiveAvgMs = frame.avgRawMs * (curVol * curVol);

      let avgLufs = -70;
      if (curVol > 0.0001 && effectiveAvgMs > 1e-9) {
        avgLufs = 10 * Math.log10(effectiveAvgMs) - 0.691;
      }

      sendResponse({
        success: true,
        bypassed: false,
        lufs: Math.max(-70, Math.round(avgLufs * 10) / 10),
        peak: Math.min(1.0, effectivePeak),
        rawRms: frame.rawRms,
        rawMs: frame.rawMs,
        avgRawRms: frame.avgRawRms
      });
      return true;
    }
  });
})();
