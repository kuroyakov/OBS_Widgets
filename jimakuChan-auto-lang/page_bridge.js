/**
 * page_bridge.js
 * 音声認識字幕ちゃん (jimakuChan v2 / v1) のページコンテキストで動作するブリッジ。
 * 1. CommentViewer から通知されたターゲット翻訳言語に合わせて「翻訳 1」の <select> を自動切替
 *    （※現在の選択値と実際に異なる場合のみ change イベントを発火し、不要な音声認識リセットを防止）
 * 2. OBS WebSocket (ws://localhost:4455) 経由で OBS ブラウザソース (CommentViewer) からの
 *    翻訳リクエスト (CHROME_TRANSLATE_REQ) を受け取り、Chrome 側で日本語へ翻訳して返却
 */
(() => {
  'use strict';

  if (window !== window.top) {
    return;
  }

  const OBS_WS_URL = 'ws://localhost:4455';
  let OBS_WS_PASS = '';

  async function fetchObsPasswordFromApi() {
    try {
      const res = await fetch('http://localhost:8080/api/config', { cache: 'no-store' });
      if (res.ok) {
        const data = await res.json();
        if (data.obs_password) {
          OBS_WS_PASS = data.obs_password;
        }
      }
    } catch (e) {}
  }

  const LANG_META = {
    'en':    { code: 'en',    labelJa: '英語',         aliases: ['en', 'en-US', 'en-GB', '英語', 'english'] },
    'ko':    { code: 'ko',    labelJa: '韓国語',       aliases: ['ko', 'ko-KR', '韓国語', 'korean'] },
    'zh-CN': { code: 'zh-CN', labelJa: '中国語',       aliases: ['zh-CN', 'zh', 'zh-Hans', '中国語', '中国語(簡体)', '簡体字', 'chinese'] },
    'zh-TW': { code: 'zh-TW', labelJa: '台湾語',       aliases: ['zh-TW', 'zh-Hant', '台湾語', '中国語(繁体)', '繁体字'] },
    'zh-HK': { code: 'zh-HK', labelJa: '香港語',       aliases: ['zh-HK', '香港語', '広東語'] },
    'fr':    { code: 'fr',    labelJa: 'フランス語',   aliases: ['fr', 'fr-FR', 'フランス語', 'french'] },
    'it':    { code: 'it',    labelJa: 'イタリア語',   aliases: ['it', 'it-IT', 'イタリア語', 'italian'] },
    'de':    { code: 'de',    labelJa: 'ドイツ語',     aliases: ['de', 'de-DE', 'ドイツ語', 'german'] },
    'es':    { code: 'es',    labelJa: 'スペイン語',   aliases: ['es', 'es-ES', 'es-MX', 'スペイン語', 'spanish'] },
    'pt':    { code: 'pt',    labelJa: 'ポルトガル語', aliases: ['pt', 'pt-BR', 'pt-PT', 'ポルトガル語', 'portuguese'] },
    'ru':    { code: 'ru',    labelJa: 'ロシア語',     aliases: ['ru', 'ru-RU', 'ロシア語', 'russian'] },
    'tr':    { code: 'tr',    labelJa: 'トルコ語',     aliases: ['tr', 'tr-TR', 'トルコ語', 'turkish'] },
    'sv':    { code: 'sv',    labelJa: 'スウェーデン語', aliases: ['sv', 'sv-SE', 'スウェーデン語', 'swedish'] },
    'pl':    { code: 'pl',    labelJa: 'ポーランド語', aliases: ['pl', 'pl-PL', 'ポーランド語', 'polish'] },
    'uk':    { code: 'uk',    labelJa: 'ウクライナ語', aliases: ['uk', 'uk-UA', 'ウクライナ語', 'ukrainian'] },
    'nl':    { code: 'nl',    labelJa: 'オランダ語',   aliases: ['nl', 'nl-NL', 'オランダ語', 'dutch'] },
    'id':    { code: 'id',    labelJa: 'インドネシア語', aliases: ['id', 'id-ID', 'インドネシア語', 'indonesian'] },
    'vi':    { code: 'vi',    labelJa: 'ベトナム語',   aliases: ['vi', 'vi-VN', 'ベトナム語', 'vietnamese'] },
    'th':    { code: 'th',    labelJa: 'タイ語',       aliases: ['th', 'th-TH', 'タイ語', 'thai'] },
    'ar':    { code: 'ar',    labelJa: 'アラビア語',   aliases: ['ar', 'ar-SA', 'アラビア語', 'arabic'] },
    'hi':    { code: 'hi',    labelJa: 'ヒンディー語', aliases: ['hi', 'hi-IN', 'ヒンディー語', 'hindi'] },
    'so':    { code: 'so',    labelJa: 'ソマリ語',     aliases: ['so', 'ソマリ語', 'somali'] },
    'el':    { code: 'el',    labelJa: 'ギリシャ語',   aliases: ['el', 'el-GR', 'ギリシャ語', 'greek'] }
  };

  function normalizeTargetLang(rawCode) {
    if (!rawCode) return 'en';
    const clean = String(rawCode).trim();
    const lower = clean.toLowerCase();
    if (lower === 'ja' || lower === 'jp' || lower === 'auto' || lower === 'unknown' || lower === 'error') return 'en';
    if (lower.startsWith('zh-tw') || lower.startsWith('zh-hant')) return 'zh-TW';
    if (lower.startsWith('zh-hk')) return 'zh-HK';
    if (lower.startsWith('zh')) return 'zh-CN';
    const base = lower.split('-')[0];
    for (const key of Object.keys(LANG_META)) {
      if (key.toLowerCase() === lower || key.toLowerCase() === base) {
        return key;
      }
    }
    return LANG_META[clean] ? clean : 'en';
  }

  function findTranslation1SelectElement() {
    const directIds = [
      'trans1', 'trans_1', 'translate1', 'select_translate1_language',
      'trans_select_1', 'transLang1', 'trans-lang-1'
    ];
    for (const id of directIds) {
      const el = document.getElementById(id);
      if (el && el.tagName === 'SELECT') return el;
    }

    const allSelects = Array.from(document.querySelectorAll('select'));
    const transSelects = allSelects.filter(sel => {
      const texts = Array.from(sel.options).map(o => (o.textContent || '').trim());
      const vals = Array.from(sel.options).map(o => (o.value || '').trim().toLowerCase());
      // 「翻訳言語(1〜3)」には必ず「なし (none)」選択肢がある（「音声認識言語」には「なし」が無い）
      const hasNone = texts.some(t => t === 'なし' || t.toLowerCase() === 'none') ||
                      vals.some(v => v === 'none' || v === '' || v === '0');
      const hasLangs = texts.some(t => t.includes('英語') || t.includes('フランス語') || t.includes('English')) ||
                       vals.some(v => v === 'en' || v === 'fr');
      return hasNone && hasLangs;
    });

    if (transSelects.length > 0) {
      return transSelects[0]; // 1番目が「翻訳 1」
    }
    return null;
  }

  function applyLanguageToJimakuChan(rawLangCode) {
    const targetKey = normalizeTargetLang(rawLangCode);
    const meta = LANG_META[targetKey] || LANG_META['en'];
    const selectEl = findTranslation1SelectElement();

    if (!selectEl) {
      return { applied: false, reason: 'select_not_found', targetKey, labelJa: meta.labelJa };
    }

    const options = Array.from(selectEl.options);
    let matchedOption = null;

    for (const opt of options) {
      const val = (opt.value || '').trim();
      const valLower = val.toLowerCase();
      const txt = (opt.textContent || '').trim();
      const txtLower = txt.toLowerCase();

      if (
        meta.aliases.some(alias => {
          const al = alias.toLowerCase();
          return valLower === al || txtLower === al || txt.includes(alias);
        })
      ) {
        matchedOption = opt;
        break;
      }
    }

    if (!matchedOption) {
      return { applied: false, reason: 'option_not_found', targetKey, labelJa: meta.labelJa };
    }

    // 【重要】すでに同じ言語が選択されている場合は change イベントを発火しない（音声認識の再起動・停止を防ぐ）
    if (selectEl.value !== matchedOption.value) {
      selectEl.value = matchedOption.value;
      selectEl.dispatchEvent(new Event('input', { bubbles: true }));
      selectEl.dispatchEvent(new Event('change', { bubbles: true }));
    }

    return {
      applied: true,
      targetKey,
      labelJa: meta.labelJa,
      optionValue: matchedOption.value,
      optionText: matchedOption.textContent.trim()
    };
  }

  /* ========================================================
   * Chrome内蔵 Translator API (window.Translator / window.LanguageDetector)
   * ======================================================== */
  const originalCreate = (typeof window.Translator !== 'undefined' && window.Translator && typeof window.Translator.create === 'function')
    ? window.Translator.create.bind(window.Translator)
    : null;
  const originalAvailability = (typeof window.Translator !== 'undefined' && window.Translator && typeof window.Translator.availability === 'function')
    ? window.Translator.availability.bind(window.Translator)
    : null;

  const originalDetectorCreate = (typeof window.LanguageDetector !== 'undefined' && window.LanguageDetector && typeof window.LanguageDetector.create === 'function')
    ? window.LanguageDetector.create.bind(window.LanguageDetector)
    : null;
  const originalDetectorAvailability = (typeof window.LanguageDetector !== 'undefined' && window.LanguageDetector && typeof window.LanguageDetector.availability === 'function')
    ? window.LanguageDetector.availability.bind(window.LanguageDetector)
    : null;

  const fallbackCache = new Map();
  async function gtxTranslate(text, sourceLang, targetLang) {
    const trimmed = (text || '').trim();
    if (!trimmed) return '';
    const cacheKey = `${sourceLang || 'auto'}->${targetLang || 'en'}:${trimmed}`;
    if (fallbackCache.has(cacheKey)) {
      return fallbackCache.get(cacheKey);
    }
    const url = `https://translate.googleapis.com/translate_a/single?client=gtx&sl=${encodeURIComponent(sourceLang || 'ja')}&tl=${encodeURIComponent(targetLang || 'en')}&dt=t&q=${encodeURIComponent(trimmed)}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`GTX HTTP ${res.status}`);
    const data = await res.json();
    const chunks = (Array.isArray(data) && Array.isArray(data[0])) ? data[0] : [];
    const out = chunks.map(c => (Array.isArray(c) && c[0]) ? c[0] : '').join('').trim() || trimmed;
    if (fallbackCache.size > 200) {
      fallbackCache.delete(fallbackCache.keys().next().value);
    }
    fallbackCache.set(cacheKey, out);
    return out;
  }

  function createFallbackTranslator(sourceLanguage, targetLanguage) {
    return {
      sourceLanguage,
      targetLanguage,
      translate: async (inputText) => {
        return await gtxTranslate(inputText, sourceLanguage, targetLanguage);
      },
      destroy: () => {}
    };
  }

  if (originalCreate) {
    if (originalAvailability) {
      window.Translator.availability = async (opts) => {
        try {
          const status = await originalAvailability(opts);
          if (status === 'available' || status === 'readily') return status;
          return 'available';
        } catch (e) {
          return 'available';
        }
      };
    }

    window.Translator.create = async (opts = {}) => {
      const src = opts.sourceLanguage || 'ja';
      const tgt = opts.targetLanguage || 'en';
      try {
        if (originalAvailability) {
          const st = await originalAvailability({ sourceLanguage: src, targetLanguage: tgt });
          if (st === 'available' || st === 'readily') {
            return await originalCreate(opts);
          }
        }
        return createFallbackTranslator(src, tgt);
      } catch (e) {
        return createFallbackTranslator(src, tgt);
      }
    };
  }

  /* ========================================================
   * CommentViewer からの翻訳リクエスト (外国語 -> 日本語) 処理
   * ======================================================== */
  const toJaTranslatorMap = new Map();
  let detectorInstancePromise = null;

  async function detectSourceLanguageInChrome(text, hintLang = 'en') {
    const fallback = (hintLang || 'en').toLowerCase();
    if (!originalDetectorCreate) return fallback;
    try {
      if (originalDetectorAvailability) {
        const dst = await originalDetectorAvailability();
        if (dst !== 'available' && dst !== 'readily') return fallback;
      }
      if (!detectorInstancePromise) {
        detectorInstancePromise = originalDetectorCreate().catch(() => null);
      }
      const det = await detectorInstancePromise;
      if (!det || typeof det.detect !== 'function') return fallback;
      const list = await det.detect(text);
      if (Array.isArray(list) && list.length > 0 && list[0].detectedLanguage) {
        const top = String(list[0].detectedLanguage).trim().toLowerCase();
        if (top && top !== 'und' && top !== 'ja') {
          return top;
        }
      }
    } catch (e) {}
    return fallback;
  }

  async function translateToJapaneseInChrome(text, hintLang = 'en') {
    const detected = await detectSourceLanguageInChrome(text, hintLang);
    const srcLang = (detected === 'zh-cn' || detected === 'zh') ? 'zh' : detected.split('-')[0];
    const normDetected = detected.startsWith('zh') ? 'ZH-CN' : srcLang.toUpperCase();
    if (!srcLang || srcLang === 'ja') {
      return { translated: text, detectedLang: 'JA', engine: 'passthrough' };
    }

    if (originalCreate && originalAvailability) {
      try {
        const st = await originalAvailability({ sourceLanguage: srcLang, targetLanguage: 'ja' });
        if (st === 'available' || st === 'readily') {
          const pairKey = `${srcLang}->ja`;
          if (!toJaTranslatorMap.has(pairKey)) {
            const p = originalCreate({ sourceLanguage: srcLang, targetLanguage: 'ja' }).catch((err) => {
              toJaTranslatorMap.delete(pairKey);
              throw err;
            });
            toJaTranslatorMap.set(pairKey, p);
          }
          const translator = await toJaTranslatorMap.get(pairKey);
          const translated = (await translator.translate(text) || '').trim();
          if (translated) {
            return { translated, detectedLang: normDetected, engine: 'chrome-builtin' };
          }
        }
      } catch (e) {}
    }

    const fallbackTranslated = await gtxTranslate(text, srcLang, 'ja');
    return {
      translated: fallbackTranslated,
      detectedLang: normDetected,
      engine: 'chrome-fallback'
    };
  }

  /* ========================================================
   * OBS WebSocket ブリッジ（window.WebSocket 本体は一切書き換えず、独立ソケットで待受）
   * ======================================================== */
  const processedReqIds = new Set();
  let bridgeObsWs = null;
  let bridgeObsAuthenticated = false;
  let bridgeReconnectTimer = null;

  function sendObsBroadcastCustomEvent(eventData) {
    if (!bridgeObsWs || !bridgeObsAuthenticated || bridgeObsWs.readyState !== WebSocket.OPEN) {
      return false;
    }
    try {
      bridgeObsWs.send(JSON.stringify({
        op: 6,
        d: {
          requestType: 'BroadcastCustomEvent',
          requestId: 'bridge-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6),
          requestData: { eventData }
        }
      }));
      return true;
    } catch (e) {
      return false;
    }
  }

  async function handleIncomingObsCustomEvent(ed) {
    if (!ed || typeof ed !== 'object') return;

    if (ed.realm === 'obs-subtitle-lang-v1' && ed.type === 'FOREIGN_COMMENT_LANG') {
      window.postMessage({
        type: 'OBS_WIDGETS_WS_FOREIGN_LANG',
        lang: ed.lang,
        reset: Boolean(ed.reset),
        timestamp: ed.timestamp || Date.now()
      }, '*');
      return;
    }

    if (ed.realm === 'obs-chrome-translate-v1' && ed.type === 'CHROME_TRANSLATE_REQ' && ed.reqId && ed.text) {
      if (processedReqIds.has(ed.reqId)) return;
      processedReqIds.add(ed.reqId);
      if (processedReqIds.size > 200) {
        const first = processedReqIds.values().next().value;
        processedReqIds.delete(first);
      }

      try {
        const res = await translateToJapaneseInChrome(ed.text, ed.hintLang || 'EN');
        sendObsBroadcastCustomEvent({
          realm: 'obs-chrome-translate-v1',
          type: 'CHROME_TRANSLATE_RES',
          reqId: ed.reqId,
          ok: true,
          translated: res.translated,
          detectedLang: res.detectedLang,
          engine: res.engine
        });
      } catch (err) {
        sendObsBroadcastCustomEvent({
          realm: 'obs-chrome-translate-v1',
          type: 'CHROME_TRANSLATE_RES',
          reqId: ed.reqId,
          ok: false,
          error: String(err && err.message ? err.message : err)
        });
      }
    }
  }

  async function sha256Base64(inputStr) {
    const bytes = new TextEncoder().encode(inputStr);
    const digest = await window.crypto.subtle.digest('SHA-256', bytes);
    let binary = '';
    const arr = new Uint8Array(digest);
    for (let i = 0; i < arr.length; i++) {
      binary += String.fromCharCode(arr[i]);
    }
    return window.btoa(binary);
  }

  async function connectDedicatedObsBridge() {
    if (!OBS_WS_PASS) {
      await fetchObsPasswordFromApi();
    }
    if (bridgeReconnectTimer) {
      clearTimeout(bridgeReconnectTimer);
      bridgeReconnectTimer = null;
    }
    if (bridgeObsWs) {
      try { bridgeObsWs.close(); } catch (e) {}
      bridgeObsWs = null;
      bridgeObsAuthenticated = false;
    }

    try {
      const ws = new WebSocket(OBS_WS_URL);
      bridgeObsWs = ws;

      ws.addEventListener('message', async (ev) => {
        try {
          const packet = JSON.parse(ev.data);
          if (!packet) return;

          if (packet.op === 0 && packet.d) {
            let authString = undefined;
            if (packet.d.authentication && packet.d.authentication.challenge && packet.d.authentication.salt) {
              const secret = await sha256Base64(OBS_WS_PASS + packet.d.authentication.salt);
              authString = await sha256Base64(secret + packet.d.authentication.challenge);
            }
            const identifyPayload = {
              op: 1,
              d: { rpcVersion: 1 }
            };
            if (authString) {
              identifyPayload.d.authentication = authString;
            }
            ws.send(JSON.stringify(identifyPayload));
          } else if (packet.op === 2) {
            bridgeObsAuthenticated = true;
          } else if (packet.op === 5 && packet.d && packet.d.eventType === 'CustomEvent') {
            handleIncomingObsCustomEvent(packet.d.eventData);
          }
        } catch (e) {}
      });

      ws.addEventListener('close', () => {
        bridgeObsAuthenticated = false;
        bridgeReconnectTimer = setTimeout(connectDedicatedObsBridge, 5000);
      });

      ws.addEventListener('error', () => {
        try { ws.close(); } catch (e) {}
      });
    } catch (e) {
      bridgeReconnectTimer = setTimeout(connectDedicatedObsBridge, 6000);
    }
  }

  // 字幕ちゃん自身の初期化を一切邪魔しないよう、ページ読み込み完了後に接続を開始
  if (document.readyState === 'complete') {
    setTimeout(connectDedicatedObsBridge, 1500);
  } else {
    window.addEventListener('load', () => setTimeout(connectDedicatedObsBridge, 1500), { once: true });
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window || !event.data) return;
    if (event.data.type === 'OBS_WIDGETS_SET_JIMAKU_LANG') {
      const result = applyLanguageToJimakuChan(event.data.lang);
      window.postMessage({
        type: 'OBS_WIDGETS_JIMAKU_LANG_RESULT',
        result,
        state: event.data.state
      }, '*');
    }
  });

  // 音声認識字幕ちゃんの確定発話を非破壊で取得し、VoiceCommand 用に content.js へ中継
  (function installSpeechRecognitionHook() {
    try {
      const attached = new WeakSet();
      const handleResult = (event) => {
        try {
          if (!event || !event.results) return;
          const startIdx = typeof event.resultIndex === 'number' ? event.resultIndex : 0;
          for (let i = startIdx; i < event.results.length; i++) {
            const res = event.results[i];
            if (res && res.isFinal && res[0] && typeof res[0].transcript === 'string') {
              const transcript = res[0].transcript.trim();
              if (transcript) {
                window.postMessage({
                  type: 'OBS_WIDGETS_JIMAKU_UTTERANCE',
                  text: transcript
                }, '*');
              }
            }
          }
        } catch (e) {}
      };
      ['SpeechRecognition', 'webkitSpeechRecognition'].forEach((ctorName) => {
        const Ctor = window[ctorName];
        if (!Ctor || !Ctor.prototype || typeof Ctor.prototype.start !== 'function') return;
        const origStart = Ctor.prototype.start;
        Ctor.prototype.start = function () {
          try {
            if (!attached.has(this)) {
              attached.add(this);
              this.addEventListener('result', handleResult);
            }
          } catch (e) {}
          return origStart.apply(this, arguments);
        };
      });
    } catch (e) {}
  })();
})();
