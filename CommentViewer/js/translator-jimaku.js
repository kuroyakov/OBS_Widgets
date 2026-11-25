// ========================================================
// 外国語→日本語変換パイプライン ＆ 音声認識字幕ちゃん連動 (js/translator-jimaku.js)
// ========================================================
'use strict';


/* ========================================================
 * STEP 1: コメント取得後の日本語変換パイプライン
 * （ここで外国語コメントを確実に日本語へ変換してから、ビュワー表示とVOICEVOX読み上げの両方に流す）
 * ======================================================== */

// 翻訳結果キャッシュ（同じ英文・テスト文面でのAPI連打や429レート制限を防止）
const translationMemoryCache = new Map();
const TRANSLATION_CACHE_MAX = 200;
let browserGtxCooldownUntil = 0;

// Chrome内蔵オンデバイス翻訳 (window.Translator / window.LanguageDetector) インスタンスキャッシュ
const chromeTranslatorCache = new Map();
let chromeLanguageDetectorPromise = null;

// OBS WebSocket 経由で本物の Google Chrome (jimakuChan-auto-lang 拡張) に Chrome内蔵翻訳を代行してもらうリクエスト管理
const pendingChromeBridgeTranslations = new Map();

// 翻訳ヘルス可視化 (Issue #38)
// 多段フォールバックは全段が劣化しても静かに辞書変換へ落ちるため、配信中に
// 「翻訳経路が死んでいる」ことに気づけない。ステージ別の成功と全段失敗の連続を数え、
// 全段失敗が閾値回数続いたら HUD の翻訳ドットを赤 (.dot.err) にして知らせる。
// 実ステージのいずれかが成功すれば復旧する。キャッシュ／スラング辞書のヒットは
// ライブ経路の健全性とは無関係なので数えない。
const TRANSLATE_DEGRADED_THRESHOLD = 3;
const TRANSLATE_STAGE_LABELS = {
  chrome: 'Chrome内蔵',
  bridge: 'Chrome拡張ブリッジ',
  google: 'Google翻訳',
  server: 'サーバー',
  mymemory: 'MyMemory'
};
const translationHealth = {
  consecutiveFailures: 0,
  degraded: false,
  stageSuccessCounts: {}
};

function noteTranslationStageSuccess(stage) {
  translationHealth.stageSuccessCounts[stage] = (translationHealth.stageSuccessCounts[stage] || 0) + 1;
  translationHealth.consecutiveFailures = 0;
  if (dotTranslate) {
    dotTranslate.title = `翻訳: 正常 (${TRANSLATE_STAGE_LABELS[stage] || stage})`;
  }
  if (translationHealth.degraded) {
    translationHealth.degraded = false;
    if (dotTranslate) dotTranslate.className = 'dot ok';
    setLog(`🌐 翻訳が復旧しました (${TRANSLATE_STAGE_LABELS[stage] || stage})`);
  }
}

function noteTranslationFailure() {
  translationHealth.consecutiveFailures += 1;
  if (dotTranslate) {
    dotTranslate.title = `翻訳: 全ステージ失敗が${translationHealth.consecutiveFailures}回連続しています`;
  }
  if (!translationHealth.degraded && translationHealth.consecutiveFailures >= TRANSLATE_DEGRADED_THRESHOLD) {
    translationHealth.degraded = true;
    setLog('⚠️ 翻訳の全ステージが連続で失敗しています');
  }
  // 劣化中は、最終フォールバック側が付けた ok/warn を赤で上書きし続ける
  // （実ステージが成功して noteTranslationStageSuccess() が呼ばれるまで赤を維持する）
  if (translationHealth.degraded && dotTranslate) {
    dotTranslate.className = 'dot err';
  }
}

function saveTranslationCache(rawKey, resultObj) {
  if (!rawKey || !resultObj) return;
  if (translationMemoryCache.has(rawKey)) {
    translationMemoryCache.delete(rawKey);
  }
  translationMemoryCache.set(rawKey, resultObj);
  if (translationMemoryCache.size > TRANSLATION_CACHE_MAX) {
    const oldestKey = translationMemoryCache.keys().next().value;
    translationMemoryCache.delete(oldestKey);
  }
}

function inferForeignLanguageFromText(text) {
  if (!text) return 'EN';
  if (/[\uAC00-\uD7AF\u1100-\u11FF]/.test(text)) return 'KO';
  if (/[\u0400-\u04FF]/.test(text)) return 'RU';
  if (/[\u0600-\u06FF]/.test(text)) return 'AR';
  if (/[\u0E00-\u0E7F]/.test(text)) return 'TH';
  if (/[\u4E00-\u9FFF]/.test(text) && !/[\u3040-\u309F\u30A0-\u30FF]/.test(text)) return 'ZH-CN';
  const low = ` ${text.toLowerCase()} `;
  if (/[àâçéèêëîïôùûüœ]/.test(low) || /\b(tu|vous|suis|est|combien|merci|bonjour|depuis)\b/.test(low)) return 'FR';
  if (/[ñ¿¡]/.test(low) || /\b(hola|gracias|cómo|estas|muy)\b/.test(low)) return 'ES';
  if (/[äöüß]/.test(low) || /\b(ich|nicht|danke|gut)\b/.test(low)) return 'DE';
  if (/[ãõ]/.test(low) || /\b(obrigado|muito|bom)\b/.test(low)) return 'PT';
  return 'EN';
}

/* ========================================================
 * ステージ0-A: ローカル Chrome 内蔵 (高速・無料・オンデバイス) 翻訳 API
 * CommentViewer 自体を Chrome で開いている場合は直接 window.Translator を使用
 * ======================================================== */
async function detectLangWithChromeBuiltIn(text, fallbackLang = 'en') {
  try {
    const DetectorAPI = window.LanguageDetector ||
      (window.ai && window.ai.languageDetector) ||
      (window.translation && window.translation.createDetector ? { create: window.translation.createDetector.bind(window.translation) } : null);
    if (!DetectorAPI || typeof DetectorAPI.create !== 'function') {
      return fallbackLang.toLowerCase();
    }
    if (!chromeLanguageDetectorPromise) {
      chromeLanguageDetectorPromise = DetectorAPI.create().catch(() => null);
    }
    const detector = await chromeLanguageDetectorPromise;
    if (!detector || typeof detector.detect !== 'function') {
      return fallbackLang.toLowerCase();
    }
    const results = await detector.detect(text);
    if (Array.isArray(results) && results.length > 0 && results[0].detectedLanguage) {
      const top = String(results[0].detectedLanguage).trim().toLowerCase();
      if (top && top !== 'und' && top !== 'ja') {
        return top;
      }
    }
  } catch (e) {}
  return fallbackLang.toLowerCase();
}

async function translateWithChromeBuiltIn(text, hintLang = 'EN') {
  const TranslatorAPI = window.Translator ||
    (window.ai && window.ai.translator) ||
    (window.translation && window.translation.createTranslator ? { create: window.translation.createTranslator.bind(window.translation) } : null);

  if (!TranslatorAPI || typeof TranslatorAPI.create !== 'function') {
    return null;
  }

  const detectedCode = await detectLangWithChromeBuiltIn(text, hintLang);
  const sourceLanguage = detectedCode === 'zh-cn' || detectedCode === 'zh' ? 'zh' : detectedCode.split('-')[0];
  if (!sourceLanguage || sourceLanguage === 'ja') return null;

  const pairKey = `${sourceLanguage}->ja`;
  try {
    if (typeof TranslatorAPI.availability === 'function') {
      const avail = await TranslatorAPI.availability({ sourceLanguage, targetLanguage: 'ja' });
      if (avail === 'unavailable' || avail === 'no') {
        return null;
      }
    }
    if (!chromeTranslatorCache.has(pairKey)) {
      const p = TranslatorAPI.create({ sourceLanguage, targetLanguage: 'ja' }).catch((err) => {
        chromeTranslatorCache.delete(pairKey);
        throw err;
      });
      chromeTranslatorCache.set(pairKey, p);
    }
    const translator = await chromeTranslatorCache.get(pairKey);
    if (!translator || typeof translator.translate !== 'function') {
      return null;
    }
    const translated = (await translator.translate(text) || '').trim();
    return {
      translated,
      detectedLang: (detectedCode.startsWith('zh') ? 'ZH-CN' : sourceLanguage).toUpperCase()
    };
  } catch (e) {
    chromeTranslatorCache.delete(pairKey);
    return null;
  }
}

/* ========================================================
 * ステージ0-B: OBS WebSocket 経由で Chrome拡張 (jimakuChan-auto-lang) の
 * Chrome内蔵翻訳を呼び出すブリッジ関数（OBSブラウザソース CEF 用）
 * ======================================================== */
function translateViaChromeExtensionBridge(text, hintLang = 'EN', timeoutMs = 1500) {
  if (!obsForJimakuConnected || !obsForJimaku) {
    return Promise.resolve(null);
  }
  const reqId = `tr-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;

  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      pendingChromeBridgeTranslations.delete(reqId);
      resolve(null);
    }, timeoutMs);

    pendingChromeBridgeTranslations.set(reqId, { resolve, timer });

    obsForJimaku.call('BroadcastCustomEvent', {
      eventData: {
        realm: 'obs-chrome-translate-v1',
        type: 'CHROME_TRANSLATE_REQ',
        reqId,
        text,
        hintLang
      }
    }).catch(() => {
      clearTimeout(timer);
      pendingChromeBridgeTranslations.delete(reqId);
      resolve(null);
    });
  });
}

// 短い英語チャット・配信スラングの即時変換辞書（Google翻訳がそのまま英語で返しがちな語句を確実に日本語化）
const CHAT_SLANG_MAP = {
  'hi': 'やあ！',
  'hey': 'やあ！',
  'hello': 'こんにちは！',
  'yo': 'よっ！',
  'sup': 'どうも！',
  'good morning': 'おはようございます！',
  'gm': 'おはよう！',
  'good evening': 'こんばんは！',
  'good night': 'おやすみなさい！',
  'gn': 'おやすみ！',
  'gg': 'グッドゲーム！',
  'ggs': 'グッドゲーム！',
  'good game': 'グッドゲーム！',
  'wp': 'ナイスプレイ！',
  'well played': 'ナイスプレイ！',
  'ggwp': 'グッドゲーム、ナイスプレイ！',
  'nt': 'ナイストライ！',
  'nice try': 'ナイストライ！',
  'glhf': 'グッドラック、楽しんで！',
  'gl': 'グッドラック！',
  'hf': '楽しんで！',
  'nice': 'ナイス！',
  'nice play': 'ナイスプレイ！',
  'cool': 'かっこいい！',
  'awesome': '最高！',
  'amazing': 'すごい！',
  'insane': 'やばすぎる！',
  'lets go': 'いくぞ！',
  "let's go": 'いくぞ！',
  'lets gooo': 'いくぞー！',
  'lol': '笑',
  'lmao': '爆笑',
  'rofl': '大爆笑',
  'haha': 'あはは',
  'hahaha': 'あはは！',
  'omg': 'なんてこった！',
  'wtf': 'なんてこった！',
  'wow': 'わお！',
  'yay': 'やったー！',
  'hype': '盛り上がる！',
  'pog': 'すごい！',
  'poggers': 'すごすぎる！',
  'clutch': 'クラッチ！神プレイ！',
  'ez': 'イージー！楽勝！',
  'rip': 'ご愁傷さま…',
  'oof': 'うわぁ…',
  'brb': 'すぐ戻ります',
  'afk': '離席中',
  'wb': 'おかえりなさい！',
  'welcome back': 'おかえりなさい！',
  'ty': 'ありがとう！',
  'thx': 'ありがとう！',
  'thanks': 'ありがとう！',
  'thank you': 'ありがとうございます！',
  'tysm': '本当にありがとう！',
  'np': 'どういたしまして！',
  'no problem': '問題ないよ！',
  'yes': 'はい！',
  'yep': 'うん！',
  'yeah': 'そうそう！',
  'no': 'いいえ',
  'nope': 'いや、違うよ',
  'ok': 'オーケー！',
  'okay': 'オーケー！',
  'test': 'テストです',
  'testing': 'テスト中です',
  'first': '1コメ！',
  'bye': 'バイバイ！',
  'cya': 'またね！',
  'see ya': 'またね！',
  'see you': 'またね！',
  'love it': '大好き！',
  'so good': 'めっちゃ良い！',
  'good job': 'グッジョブ！',
  'gj': 'グッジョブ！'
};

// 頻出英単語のカタカナフォールバック辞書（万が一翻訳後に英単語が残った場合のVOICEVOX英語読み防止）
const EN_WORD_KATAKANA = {
  'hello': 'ハロー',
  'from': 'フロム',
  'california': 'カリフォルニア',
  'your': 'あなたの',
  'keyboard': 'キーボード',
  'chord': 'コード',
  'playing': '演奏',
  'sounds': 'サウンド',
  'amazing': '素晴らしい',
  'stream': '配信',
  'music': 'ミュージック',
  'game': 'ゲーム',
  'splatoon': 'スプラトゥーン',
  'minecraft': 'マインクラフト',
  'dungeons': 'ダンジョンズ',
  'nice': 'ナイス',
  'good': 'グッド',
  'great': 'グレート',
  'super': 'スーパー',
  'thank': 'サンキュー',
  'thanks': 'サンクス',
  'you': 'ユー',
  'very': 'ベリー',
  'much': 'マッチ',
  'love': 'ラブ',
  'like': 'ライク',
  'watch': 'ウォッチ',
  'watching': '視聴中',
  'follow': 'フォロー',
  'followed': 'フォローしました',
  'sub': 'サブスク',
  'subscribed': 'サブスクしました',
  'raid': 'レイド',
  'cheer': 'チアー',
  'bits': 'ビッツ',
  'usa': 'アメリカ',
  'japan': 'ジャパン',
  'japanese': '日本語',
  'english': '英語'
};

function needsJapaneseTranslation(text) {
  if (!text) return false;
  // URLや数字・記号・w・8を除いた文字を調べる
  const cleaned = text
    .replace(/https?:\/\/\S+/gi, '')
    .replace(/[0-9\s!-/:-@\[-`{-~！？。、…「」『』（）【】・ーｗwW８8]+/g, '');
  if (!cleaned) return false;

  // ひらがな・カタカナ（長音「ー」や中黒「・」を除く、半角カタカナ含む）の数をカウント
  const jpMatches = cleaned.match(/[\u3041-\u3096\u30A1-\u30FA\u30FD-\u30FF\uFF66-\uFF9D]/g) || [];
  // ラテン文字（英語等）・ハングル・キリル文字の数をカウント
  const foreignMatches = cleaned.match(/[A-Za-z\u00C0-\u024F\u0400-\u04FF\uAC00-\uD7AF]/g) || [];

  // ひらがな・カタカナが1文字も無ければ（英語・韓国語・中国語など）必ず翻訳
  if (jpMatches.length === 0) {
    return true;
  }
  // 英語などの外国語文字が含まれており、かつ2文字以上の英単語がある場合は翻訳
  if (foreignMatches.length >= 2 && /[A-Za-z]{2,}/.test(cleaned)) {
    // 日本語文字より外国語文字の方が多い場合、または英語フレーズが含まれる場合
    if (foreignMatches.length >= jpMatches.length) {
      return true;
    }
  }
  return false;
}

function replaceRemainingEnglishWithJapanese(text) {
  if (!text) return '';
  return text.replace(/\b([A-Za-z]{2,})\b/g, (match) => {
    const lower = match.toLowerCase();
    if (CHAT_SLANG_MAP[lower]) return CHAT_SLANG_MAP[lower];
    if (EN_WORD_KATAKANA[lower]) return EN_WORD_KATAKANA[lower];
    return match;
  });
}

// 翻訳結果が「記号やスペースが変わっただけ」ではなく、実際に日本語へ変換されたかを厳密に判定
function isValidJapaneseTranslation(originalText, translatedText) {
  if (!translatedText) return false;
  const stripPunct = (s) => s.toLowerCase().replace(/[\s!-/:-@\[-`{-~！？。、…「」『』（）【】・ー]+/g, '');
  if (stripPunct(originalText) === stripPunct(translatedText)) {
    return false; // スペースや疑問符・大文字小文字が変わっただけの場合は未翻訳とみなす
  }
  // 翻訳結果にひらがな・カタカナ・漢字のいずれかが含まれていることを確認
  return /[\u3041-\u3096\u30A1-\u30FA\u30FD-\u30FF\u4E00-\u9FFF]/.test(translatedText);
}

// ブラウザ直接 Google Translate API 呼び出し（429時は即クールダウン設定）
async function fetchGoogleTranslateDirect(text, sourceLang = 'auto') {
  const url = `https://translate.googleapis.com/translate_a/single?client=gtx&sl=${encodeURIComponent(sourceLang)}&tl=ja&dt=t&dt=bd&q=${encodeURIComponent(text)}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3500);
  try {
    const res = await fetch(url, { signal: controller.signal });
    if (res.status === 429) {
      browserGtxCooldownUntil = Date.now() + 60000;
      throw new Error('GTX HTTP 429');
    }
    if (!res.ok) throw new Error(`GTX HTTP ${res.status}`);
    const data = await res.json();
    const chunks = (Array.isArray(data) && Array.isArray(data[0])) ? data[0] : [];
    let translated = chunks.map(c => (Array.isArray(c) && c[0]) ? c[0] : '').join('').trim();
    const detectedLang = (Array.isArray(data) && typeof data[2] === 'string') ? data[2] : sourceLang;

    // もし1単語等で dt=t が原文のまま返した場合、辞書データ (data[1]) に日本語訳があればそちらを採用
    if (
      !isValidJapaneseTranslation(text, translated) &&
      Array.isArray(data[1]) &&
      data[1].length > 0 &&
      Array.isArray(data[1][0]) &&
      Array.isArray(data[1][0][1]) &&
      data[1][0][1].length > 0
    ) {
      translated = data[1][0][1][0];
    }
    return { translated, detectedLang };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * コメントテキストを受け取り、外国語であれば日本語に変換して返す統合関数。
 * この関数の戻り値（日本語テキスト）をビュワー表示とVOICEVOX読み上げの両方に使用する。
 */
async function convertCommentToJapanese(rawText) {
  const trimmed = (rawText || '').trim();
  if (!trimmed || !settings.translate_enabled) {
    return { text: trimmed, isTranslated: false, lang: 'JA' };
  }

  // 0. メモリキャッシュに翻訳済み結果があれば即座に返す（同一文面の再翻訳・429を完全防止）
  if (translationMemoryCache.has(trimmed)) {
    const cached = translationMemoryCache.get(trimmed);
    dotTranslate.className = 'dot ok';
    setLog(`🌐 翻訳(キャッシュ/${cached.lang}): "${cached.text.slice(0, 22)}"`);
    return cached;
  }

  // 1. 定番の英語スラング・短文に完全一致する場合は即座に日本語へ変換
  const normalizedKey = trimmed.toLowerCase().replace(/[!?.！？。、]+$/g, '').trim();
  if (CHAT_SLANG_MAP[normalizedKey]) {
    const slangJa = CHAT_SLANG_MAP[normalizedKey];
    const resObj = { text: slangJa, isTranslated: true, lang: 'EN' };
    saveTranslationCache(trimmed, resObj);
    setLog(`🌐 翻訳(辞書): "${trimmed}" → "${slangJa}"`);
    dotTranslate.className = 'dot ok';
    return resObj;
  }

  // 2. 日本語判定（ひらがな・カタカナ主体の日本語コメントならそのまま返す）
  if (!needsJapaneseTranslation(trimmed)) {
    return { text: trimmed, isTranslated: false, lang: 'JA' };
  }

  let detectedSourceLang = inferForeignLanguageFromText(trimmed);

  // 3-A. ステージ0-A: ローカル Chrome 内蔵オンデバイス翻訳 (CommentViewer 自体を Chrome で開いている場合)
  const chromeBuiltInRes = await translateWithChromeBuiltIn(trimmed, detectedSourceLang);
  if (chromeBuiltInRes && isValidJapaneseTranslation(trimmed, chromeBuiltInRes.translated)) {
    const finalJa = replaceRemainingEnglishWithJapanese(chromeBuiltInRes.translated);
    const resObj = {
      text: finalJa,
      isTranslated: true,
      lang: (chromeBuiltInRes.detectedLang || detectedSourceLang || 'EN').toUpperCase()
    };
    saveTranslationCache(trimmed, resObj);
    dotTranslate.className = 'dot ok';
    noteTranslationStageSuccess('chrome');
    setLog(`🌐 翻訳完了(Chrome内蔵/${resObj.lang}): "${finalJa.slice(0, 22)}"`);
    return resObj;
  }

  // 3-B. ステージ0-B: OBS ブラウザソース (CEF) から OBS WebSocket 経由で
  //      裏で開いている Chrome 拡張 (jimakuChan-auto-lang) の Chrome 内蔵翻訳を呼び出すブリッジ
  const bridgeRes = await translateViaChromeExtensionBridge(trimmed, detectedSourceLang, 1500);
  if (bridgeRes && bridgeRes.ok && isValidJapaneseTranslation(trimmed, bridgeRes.translated)) {
    const finalJa = replaceRemainingEnglishWithJapanese(bridgeRes.translated);
    const resObj = {
      text: finalJa,
      isTranslated: true,
      lang: (bridgeRes.detectedLang || detectedSourceLang || 'EN').toUpperCase()
    };
    saveTranslationCache(trimmed, resObj);
    dotTranslate.className = 'dot ok';
    noteTranslationStageSuccess('bridge');
    setLog(`🌐 翻訳完了(Chrome拡張内蔵/${resObj.lang}): "${finalJa.slice(0, 22)}"`);
    return resObj;
  }

  // 4. ステージ1: ブラウザから直接 Google 翻訳 (Chromeが閉じている時などのフォールバック・429クールダウン制御付き)
  if (Date.now() >= browserGtxCooldownUntil) {
    try {
      const { translated, detectedLang } = await fetchGoogleTranslateDirect(trimmed, 'auto');
      if (detectedLang && detectedLang !== 'auto') {
        detectedSourceLang = detectedLang.toUpperCase();
      }
      if (isValidJapaneseTranslation(trimmed, translated)) {
        const finalJa = replaceRemainingEnglishWithJapanese(translated);
        const resObj = {
          text: finalJa,
          isTranslated: true,
          lang: (detectedSourceLang || 'EN').toUpperCase()
        };
        saveTranslationCache(trimmed, resObj);
        dotTranslate.className = 'dot ok';
        noteTranslationStageSuccess('google');
        setLog(`🌐 翻訳完了(${resObj.lang}): "${finalJa.slice(0, 22)}"`);
        return resObj;
      }
    } catch (errDirect) {
      // 429 等の場合は無駄なリトライをせずステージ2 (/api/translate) へ進む
    }
  }

  // 5. ステージ2: ローカルサーバー (/api/translate) 経由の翻訳フォールバック
  try {
    const res = await fetch(`${API_BASE}/api/translate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: trimmed })
    });
    if (res.ok) {
      const data = await res.json();
      if (data.detected_lang && data.detected_lang !== 'error' && data.detected_lang !== 'auto') {
        detectedSourceLang = data.detected_lang.toUpperCase();
      }
      if (isValidJapaneseTranslation(trimmed, data.translated_text)) {
        const finalJa = replaceRemainingEnglishWithJapanese(data.translated_text);
        const resObj = {
          text: finalJa,
          isTranslated: true,
          lang: (data.detected_lang || detectedSourceLang || 'EN').toUpperCase()
        };
        saveTranslationCache(trimmed, resObj);
        dotTranslate.className = 'dot ok';
        noteTranslationStageSuccess('server');
        setLog(`🌐 翻訳完了(Server/${resObj.lang}): "${finalJa.slice(0, 22)}"`);
        return resObj;
      }
    }
  } catch (errServer) {
    console.warn('Server translate fallback:', errServer);
  }

  // 6. ステージ3: MyMemory 翻訳API フォールバック
  try {
    const langPair = `${detectedSourceLang.toLowerCase()}|ja`;
    const mmUrl = `https://api.mymemory.translated.net/get?q=${encodeURIComponent(trimmed)}&langpair=${encodeURIComponent(langPair)}`;
    const mmRes = await fetch(mmUrl);
    if (mmRes.ok) {
      const mmData = await mmRes.json();
      const mmText = mmData && mmData.responseData && mmData.responseData.translatedText;
      if (isValidJapaneseTranslation(trimmed, mmText)) {
        const finalJa = replaceRemainingEnglishWithJapanese(mmText);
        const resObj = {
          text: finalJa,
          isTranslated: true,
          lang: detectedSourceLang.toUpperCase()
        };
        saveTranslationCache(trimmed, resObj);
        dotTranslate.className = 'dot ok';
        noteTranslationStageSuccess('mymemory');
        setLog(`🌐 翻訳完了(MM/${resObj.lang}): "${finalJa.slice(0, 22)}"`);
        return resObj;
      }
    }
  } catch (errMm) {}

  // 7. 最終フォールバック: 単語辞書でカタカナ・日本語化
  const fallbackJa = replaceRemainingEnglishWithJapanese(trimmed);
  const changed = isValidJapaneseTranslation(trimmed, fallbackJa);
  dotTranslate.className = changed ? 'dot ok' : 'dot warn';
  // 実ステージは全段失敗した扱い（辞書で拾えても経路の劣化として数える）。
  // ドットの色はこの後に noteTranslationFailure() が劣化状態なら赤へ上書きする。
  noteTranslationFailure();
  return {
    text: fallbackJa,
    isTranslated: changed,
    lang: detectedSourceLang.toUpperCase()
  };
}


/* ========================================================
 * 音声認識字幕ちゃん (v2) 翻訳言語の自動切替 ＆ 5分無発言タイマー制御
 * ＋ Chrome拡張からの翻訳レスポンス (CHROME_TRANSLATE_RES) 受信
 * ======================================================== */
const lblJimakuLang = document.getElementById('lbl-jimaku-lang');
const btnResetJimakuLang = document.getElementById('btn-reset-jimaku-lang');
const JIMAKU_TIMEOUT_MS = 5 * 60 * 1000; // 5分 (300,000ms)
const JIMAKU_LANG_NAMES_JA = {
  'en': '英語', 'ko': '韓国語', 'zh-CN': '中国語', 'zh-TW': '台湾語', 'zh-HK': '香港語',
  'fr': 'フランス語', 'it': 'イタリア語', 'de': 'ドイツ語', 'es': 'スペイン語',
  'pt': 'ポルトガル語', 'ru': 'ロシア語', 'tr': 'トルコ語', 'sv': 'スウェーデン語',
  'pl': 'ポーランド語', 'uk': 'ウクライナ語', 'nl': 'オランダ語', 'id': 'インドネシア語',
  'vi': 'ベトナム語', 'th': 'タイ語', 'ar': 'アラビア語', 'hi': 'ヒンディー語', 'so': 'ソマリ語', 'el': 'ギリシャ語'
};

let jimakuLangState = {
  lang: 'en',
  lastForeignCommentMs: 0
};

// OBS WebSocket 接続（jimakuChan v2 への通知 ＆ Chrome内蔵翻訳ブリッジ送受信 / shared/obs-client.js 使用）
let obsForJimaku = null;
let obsForJimakuConnected = false;

const jimakuObsClient = window.OBSWidgetsClient.createManagedObsClient({
  reconnectDelayMs: 5000,
  setupListeners: (instance) => {
    instance.on('CustomEvent', (raw) => {
      const data = (raw && raw.eventData) || raw;
      if (data && data.realm === 'obs-chrome-translate-v1' && data.type === 'CHROME_TRANSLATE_RES' && data.reqId) {
        const pending = pendingChromeBridgeTranslations.get(data.reqId);
        if (pending) {
          clearTimeout(pending.timer);
          pendingChromeBridgeTranslations.delete(data.reqId);
          pending.resolve(data);
        }
      }
    });
  },
  onConnected: (instance) => {
    obsForJimaku = instance;
    obsForJimakuConnected = true;
  },
  onDisconnected: () => {
    obsForJimakuConnected = false;
  }
});

jimakuObsClient.connect();

function normalizeJimakuLang(rawCode) {
  if (!rawCode) return 'en';
  const low = String(rawCode).trim().toLowerCase();
  if (low === 'ja' || low === 'jp' || low === 'auto' || low === 'unknown' || low === 'error' || low === 'foreign') return 'en';
  if (low.startsWith('zh-tw') || low.startsWith('zh-hant')) return 'zh-TW';
  if (low.startsWith('zh-hk')) return 'zh-HK';
  if (low.startsWith('zh')) return 'zh-CN';
  const base = low.split('-')[0];
  return JIMAKU_LANG_NAMES_JA[base] ? base : 'en';
}

function renderJimakuLangStatus() {
  if (!lblJimakuLang) return;
  const now = Date.now();
  if (jimakuLangState.lastForeignCommentMs > 0) {
    const elapsed = now - jimakuLangState.lastForeignCommentMs;
    if (elapsed >= JIMAKU_TIMEOUT_MS) {
      // 最後の外国語コメントから5分経過 → 英語(デフォルト)に復帰（OBS WebSocketとサーバーの両方を一貫してリセット）
      notifyForeignCommentToJimakuChan('en', true);
      return;
    }
    const remSec = Math.max(0, Math.ceil((JIMAKU_TIMEOUT_MS - elapsed) / 1000));
    const m = Math.floor(remSec / 60);
    const s = remSec % 60;
    const jaName = JIMAKU_LANG_NAMES_JA[jimakuLangState.lang] || jimakuLangState.lang.toUpperCase();
    lblJimakuLang.textContent = `🎬 字幕ちゃん翻訳: ${jaName} (${jimakuLangState.lang.toUpperCase()}) ⏱ ${m}:${String(s).padStart(2, '0')}`;
    lblJimakuLang.style.color = '#fde047';
  } else {
    lblJimakuLang.textContent = '🎬 字幕ちゃん翻訳: 英語 (EN / デフォルト)';
    lblJimakuLang.style.color = '#7dd3fc';
  }
}

setInterval(renderJimakuLangStatus, 1000);

async function notifyForeignCommentToJimakuChan(detectedLangCode, isReset = false) {
  if (isReset) {
    jimakuLangState.lang = 'en';
    jimakuLangState.lastForeignCommentMs = 0;
  } else {
    const norm = normalizeJimakuLang(detectedLangCode);
    jimakuLangState.lang = norm;
    // 外国語コメントが来るたびに現在時刻で5分タイマーをリセット（同じ外国語が続く場合も5分延長）
    jimakuLangState.lastForeignCommentMs = Date.now();
  }
  renderJimakuLangStatus();

  // 1. OBS WebSocket 経由で jimakuChan v2 側へ即時通知
  if (obsForJimakuConnected && obsForJimaku) {
    obsForJimaku.call('BroadcastCustomEvent', {
      eventData: {
        realm: 'obs-subtitle-lang-v1',
        type: 'FOREIGN_COMMENT_LANG',
        lang: jimakuLangState.lang,
        reset: Boolean(isReset),
        timestamp: Date.now()
      }
    }).catch(() => {});
  }

  // 2. server.py (/api/subtitle/lang) にも同期保存
  authFetch(`${API_BASE}/api/subtitle/lang`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      lang: jimakuLangState.lang,
      reset: Boolean(isReset)
    })
  }).catch(() => {});
}

if (btnResetJimakuLang) {
  btnResetJimakuLang.addEventListener('click', () => {
    notifyForeignCommentToJimakuChan('en', true);
    setLog('🎬 字幕ちゃんの翻訳言語を英語(デフォルト)にリセットしました');
  });
}
