/**
 * content.js
 * 音声認識字幕ちゃん (https://sayonari.github.io/jimakuChan/v2/ 等) 上で動作するコンテンツスクリプト。
 * - page_bridge.js をページ開始時に注入
 * - CommentViewer からの言語通知を server.py (/api/subtitle/lang) と OBS WebSocket CustomEvent の両方で受信
 * - 最後の外国語コメントから5分（300秒）経過するまでその言語を維持し、5分間外国語コメントが来なければ英語（en）に自動復帰
 * - 【重要】字幕ちゃんの音声認識セッションをリセットさせないよう、実際に翻訳言語が変化した瞬間だけ DOM 変更イベントを送出する
 */
(() => {
  'use strict';

  // 子フレーム（音声認識用の内部iframe等）では一切のポーリングやDOM書き換えを行わない（音声認識リセット防止）
  if (window !== window.top) {
    return;
  }

  const API_URL = 'http://localhost:8080/api/subtitle/lang';
  const FIVE_MIN_MS = 5 * 60 * 1000;

  // 1. page_bridge.js を親フレームにのみ注入
  function injectPageBridge() {
    try {
      const script = document.createElement('script');
      script.src = chrome.runtime.getURL('page_bridge.js');
      script.onload = () => script.remove();
      (document.head || document.documentElement).appendChild(script);
    } catch (e) {
      console.warn('jimakuChan bridge injection error:', e);
    }
  }
  injectPageBridge();

  const LANG_NAMES_JA = {
    'en': '英語', 'ko': '韓国語', 'zh-CN': '中国語', 'zh-TW': '台湾語', 'zh-HK': '香港語',
    'fr': 'フランス語', 'it': 'イタリア語', 'de': 'ドイツ語', 'es': 'スペイン語',
    'pt': 'ポルトガル語', 'ru': 'ロシア語', 'tr': 'トルコ語', 'sv': 'スウェーデン語',
    'pl': 'ポーランド語', 'uk': 'ウクライナ語', 'nl': 'オランダ語', 'id': 'インドネシア語',
    'vi': 'ベトナム語', 'th': 'タイ語', 'ar': 'アラビア語', 'hi': 'ヒンディー語', 'so': 'ソマリ語', 'el': 'ギリシャ語'
  };

  function normalizeLang(raw) {
    if (!raw) return 'en';
    const low = String(raw).trim().toLowerCase();
    if (low === 'ja' || low === 'jp' || low === 'auto' || low === 'unknown' || low === 'error') return 'en';
    if (low.startsWith('zh-tw') || low.startsWith('zh-hant')) return 'zh-TW';
    if (low.startsWith('zh-hk')) return 'zh-HK';
    if (low.startsWith('zh')) return 'zh-CN';
    const base = low.split('-')[0];
    return LANG_NAMES_JA[base] ? base : 'en';
  }

  // ローカル5分タイマー状態と「最後に字幕ちゃんへ適用した言語」の記録
  let localState = {
    lang: 'en',
    lastForeignCommentMs: 0
  };
  let lastAppliedLangToPage = 'en';

  // 2. 右下ステータスバッジUI（クリックで手動英語リセットも可能）
  let badge = null;
  let dotEl = null;
  let textEl = null;

  function ensureBadgeMounted() {
    if (badge || !document.body) return;
    badge = document.createElement('div');
    badge.id = 'obs-widgets-jimaku-badge';
    badge.style.cssText = [
      'position: fixed',
      'bottom: 12px',
      'right: 12px',
      'z-index: 2147483647',
      'background: rgba(17, 18, 24, 0.92)',
      'border: 1.5px solid #a970ff',
      'border-radius: 999px',
      'padding: 6px 14px',
      'color: #f8fafc',
      'font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      'font-size: 12px',
      'font-weight: 800',
      'display: flex',
      'align-items: center',
      'gap: 8px',
      'box-shadow: 0 6px 20px rgba(0, 0, 0, 0.65)',
      'cursor: pointer',
      'user-select: none',
      'transition: all 0.2s ease'
    ].join(';');
    badge.title = 'クリックで英語(デフォルト)に即時リセット';

    dotEl = document.createElement('span');
    dotEl.style.cssText = 'width: 8px; height: 8px; border-radius: 50%; background: #10b981; box-shadow: 0 0 6px #10b981; flex-shrink: 0;';

    textEl = document.createElement('span');
    textEl.textContent = '🌐 コメビュ連動: 英語 (EN / デフォルト)';

    badge.appendChild(dotEl);
    badge.appendChild(textEl);
    document.body.appendChild(badge);

    badge.addEventListener('click', async () => {
      localState.lang = 'en';
      localState.lastForeignCommentMs = 0;
      sendLangToPageIfChanged('en', { manualReset: true }, true);
      updateBadgeUI('en', 0);
      try {
        await fetch(API_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ lang: 'en', reset: true })
        });
      } catch (e) {}
    });
  }

  function formatMmSs(totalSec) {
    const s = Math.max(0, Math.ceil(totalSec));
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}:${String(rem).padStart(2, '0')}`;
  }

  // 実際に言語が変わった時（または手動リセット時）だけ page_bridge.js に通知を送る
  // ※毎秒のカウントダウンで change イベントを発火させると字幕ちゃんの音声認識がリセットされて止まるため
  function sendLangToPageIfChanged(langCode, stateInfo, force = false) {
    const norm = normalizeLang(langCode);
    if (!force && norm === lastAppliedLangToPage) {
      return;
    }
    lastAppliedLangToPage = norm;
    window.postMessage({
      type: 'OBS_WIDGETS_SET_JIMAKU_LANG',
      lang: norm,
      state: stateInfo
    }, '*');
  }

  function updateBadgeUI(langCode, remainingSec) {
    ensureBadgeMounted();
    if (!badge || !textEl || !dotEl) return;

    const cleanCode = langCode || 'en';
    const jaName = LANG_NAMES_JA[cleanCode] || LANG_NAMES_JA[cleanCode.split('-')[0]] || cleanCode.toUpperCase();
    const isSwitched = remainingSec > 0 && cleanCode.toLowerCase() !== 'en';

    if (isSwitched) {
      badge.style.borderColor = '#38bdf8';
      badge.style.background = 'rgba(12, 45, 72, 0.94)';
      dotEl.style.background = '#38bdf8';
      dotEl.style.boxShadow = '0 0 8px #38bdf8';
      textEl.textContent = `🌐 自動翻訳切替中: ${jaName} (${cleanCode.toUpperCase()}) ⏱ ${formatMmSs(remainingSec)}`;
    } else {
      badge.style.borderColor = '#a970ff';
      badge.style.background = 'rgba(17, 18, 24, 0.92)';
      dotEl.style.background = '#10b981';
      dotEl.style.boxShadow = '0 0 6px #10b981';
      textEl.textContent = `🌐 コメビュ連動: 英語 (EN / デフォルト)`;
    }
  }

  const VOICE_CMD_API_URL = 'http://localhost:8080/api/voice-command/utterance';
  let cmdToast = null;
  let cmdToastTimer = null;

  function showVoiceCommandToast(summary, isOk) {
    try {
      ensureBadgeMounted();
      if (!document.body) return;
      if (!cmdToast) {
        cmdToast = document.createElement('div');
        cmdToast.id = 'obs-widgets-voice-cmd-toast';
        cmdToast.style.cssText = [
          'position: fixed',
          'bottom: 54px',
          'right: 12px',
          'z-index: 2147483647',
          'background: rgba(17, 18, 24, 0.94)',
          'border: 1.5px solid #38bdf8',
          'border-radius: 10px',
          'padding: 7px 14px',
          'color: #f8fafc',
          'font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
          'font-size: 12px',
          'font-weight: 700',
          'box-shadow: 0 6px 20px rgba(0, 0, 0, 0.65)',
          'pointer-events: none',
          'transition: opacity 0.25s ease',
          'opacity: 0'
        ].join(';');
        document.body.appendChild(cmdToast);
      }
      cmdToast.style.borderColor = isOk ? '#38bdf8' : '#f87171';
      cmdToast.textContent = summary;
      cmdToast.style.opacity = '1';
      if (cmdToastTimer) clearTimeout(cmdToastTimer);
      cmdToastTimer = setTimeout(() => {
        if (cmdToast) cmdToast.style.opacity = '0';
      }, 3500);
    } catch (e) {}
  }

  async function forwardUtteranceToVoiceCommand(text) {
    try {
      const res = await fetch(VOICE_CMD_API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.executed && data.summary) {
        showVoiceCommandToast(data.summary, data.ok !== false);
      }
    } catch (e) {}
  }

  // OBS WebSocket 経由で直接届いた外国語コメントイベント・確定発話を受信
  window.addEventListener('message', (event) => {
    if (!event.data) return;
    if (event.data.type === 'OBS_WIDGETS_JIMAKU_UTTERANCE' && typeof event.data.text === 'string') {
      forwardUtteranceToVoiceCommand(event.data.text);
      return;
    }
    if (event.data.type === 'OBS_WIDGETS_WS_FOREIGN_LANG') {
      if (event.data.reset) {
        localState.lang = 'en';
        localState.lastForeignCommentMs = 0;
        sendLangToPageIfChanged('en', { reset: true }, true);
        updateBadgeUI('en', 0);
      } else {
        const norm = normalizeLang(event.data.lang);
        localState.lang = norm;
        localState.lastForeignCommentMs = Date.now();
        sendLangToPageIfChanged(norm, { remaining_sec: 300 });
        updateBadgeUI(norm, 300);
      }
    }
  });

  // 3. サーバー同期（5秒に1回のみ軽量チェック・言語が変化した時のみ反映）
  async function syncServerSubtitleLang() {
    try {
      const res = await fetch(API_URL, { cache: 'no-store' });
      if (res.ok) {
        const data = await res.json();
        const serverLang = normalizeLang(data.lang || 'en');
        const serverTsMs = Number(data.last_foreign_comment_ts || 0) * 1000;
        const remSec = Number(data.remaining_sec || 0);

        if (remSec > 0 && serverTsMs > 0) {
          localState.lang = serverLang;
          localState.lastForeignCommentMs = serverTsMs;
          sendLangToPageIfChanged(serverLang, data);
          updateBadgeUI(serverLang, remSec);
        } else if (localState.lastForeignCommentMs > 0 && serverTsMs === 0) {
          localState.lang = 'en';
          localState.lastForeignCommentMs = 0;
          sendLangToPageIfChanged('en', data, true);
          updateBadgeUI('en', 0);
        } else {
          updateBadgeUI('en', 0);
        }
      }
    } catch (e) {}
  }

  // 4. ローカルのカウントダウン表示＆5分経過判定（1秒ごと・バッジ表示のみ更新し、5分満了時のみ英語へ戻す）
  function tickLocalCountdown() {
    ensureBadgeMounted();
    if (localState.lastForeignCommentMs > 0) {
      const elapsedMs = Date.now() - localState.lastForeignCommentMs;
      if (elapsedMs >= FIVE_MIN_MS) {
        localState.lang = 'en';
        localState.lastForeignCommentMs = 0;
        sendLangToPageIfChanged('en', { reverted: true });
        updateBadgeUI('en', 0);
      } else {
        const remSec = (FIVE_MIN_MS - elapsedMs) / 1000;
        updateBadgeUI(localState.lang, remSec);
      }
    } else {
      updateBadgeUI('en', 0);
    }
  }

  setInterval(syncServerSubtitleLang, 5000);
  setInterval(tickLocalCountdown, 1000);
  // 初回起動時は字幕ちゃん自身の音声認識スタートを邪魔しないよう3秒待ってからステータス同期
  setTimeout(syncServerSubtitleLang, 3000);
})();
