// MixConsole Background Service Worker (v1.3.2)
// タブのURL変更（別動画・別コンテンツへの遷移）やリロード時に設定を自動維持・再接続する

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  // URL変更時、またはページ読み込み完了時、またはメディア再生開始時
  const isTargetTrigger = (changeInfo.status === 'complete') ||
                          (changeInfo.url !== undefined) ||
                          (changeInfo.audible === true);

  if (!isTargetTrigger) return;
  if (!tab.url || (!tab.url.startsWith('http://') && !tab.url.startsWith('https://'))) return;

  let currentOrigin = '';
  try {
    currentOrigin = new URL(tab.url).origin;
  } catch (_) {
    return;
  }

  const storageKey = `tab_settings_${tabId}`;
  const [sessionRes, localRes] = await Promise.all([
    chrome.storage.session.get([storageKey]),
    chrome.storage.local.get(['masterVol', 'masterMuted'])
  ]);

  const settings = sessionRes[storageKey];
  if (!settings) return;

  // 保存時のオリジンと一致する場合のみ自動再注入（別オリジン遷移先への注入を防止）
  if (!settings.origin || settings.origin !== currentOrigin) return;

  // もしユーザーによって明示的にバイパス(OFF)されていないなら自動再接続
  if (settings.bypassed) return;

  try {
    // 1. content.js を注入
    await chrome.scripting.executeScript({
      target: { tabId: tabId },
      files: ['content.js']
    });

    // 2. マスター音量・Preamp Trim を加味した実効音量を算出 (ミュート時は 0 を維持)
    const masterVol = typeof localRes.masterVol === 'number' ? localRes.masterVol : 1.0;
    const isMasterMuted = !!localRes.masterMuted;
    const chGain = typeof settings.channelGain === 'number'
      ? settings.channelGain
      : (typeof settings.volume === 'number' ? settings.volume : 1.0);
    const trimMult = Math.pow(10, (settings.eqTrim || 0) / 20);
    const effectiveVol = isMasterMuted ? 0 : chGain * masterVol * trimMult;
    const isEqBypassed = !!settings.eqBypassed;

    // 3. 設定パラメータを復元
    chrome.tabs.sendMessage(tabId, {
      type: 'ATTACH_AUDIO',
      pan: settings.pan ?? 0,
      volume: effectiveVol,
      bypassed: false,
      eqHpf: isEqBypassed ? 0 : (settings.eqHpf ?? 0),
      eqHigh: isEqBypassed ? 0 : (settings.eqHigh ?? 0),
      eqHighFreq: settings.eqHighFreq ?? 12000,
      eqMid: isEqBypassed ? 0 : (settings.eqMid ?? 0),
      eqMidFreq: settings.eqMidFreq ?? 1500,
      eqMidQ: settings.eqMidQ ?? 1.0,
      eqLow: isEqBypassed ? 0 : (settings.eqLow ?? 0),
      eqLowFreq: settings.eqLowFreq ?? 200
    }, () => {
      // ignore runtime errors
      if (chrome.runtime.lastError) {}
    });
  } catch (err) {
    // タブの権限がない等の場合は無視
  }
});

// タブが閉じられたら該当タブの設定のみ破棄（「タブが閉じられない限り維持」の要件）
chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove([`tab_settings_${tabId}`]);
});
