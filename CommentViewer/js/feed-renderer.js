// ========================================================
// コメント・アラートカード描画 ＆ テスト発火機能 (js/feed-renderer.js)
// ========================================================
'use strict';

/* ========================================================
 * STEP 3: 画面へのカード追加・上限管理・URL検証
 * ======================================================== */
function isSafeImageUrl(url) {
  return typeof url === 'string' && /^https?:\/\//i.test(url.trim());
}

function appendFeedElement(el) {
  stage.appendChild(el);
  const maxItems = settings.max_comments || 8;
  while (stage.children.length > maxItems) {
    stage.removeChild(stage.firstElementChild);
  }
  if (settings.hide_after_sec && settings.hide_after_sec > 0) {
    setTimeout(() => {
      el.classList.add('fading-out');
      setTimeout(() => {
        if (el.parentNode) el.remove();
      }, 420);
    }, settings.hide_after_sec * 1000);
  }
}

// プラットフォーム識別アイコンバッジ生成 (Twitch: 紫グリッチロゴ / YouTube: 赤再生ロゴ)
function createPlatformIconBadge(platform) {
  const badge = document.createElement('span');
  const isYt = String(platform || 'twitch').toLowerCase() === 'youtube';
  badge.className = `platform-badge ${isYt ? 'platform-youtube' : 'platform-twitch'}`;
  badge.title = isYt ? 'YouTube Live' : 'Twitch';
  if (isYt) {
    badge.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.5 12 3.5 12 3.5s-7.5 0-9.4.6A3 3 0 0 0 .5 6.2 31.4 31.4 0 0 0 0 12a31.4 31.4 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.6 9.4.6 9.4.6s7.5 0 9.4-.6a3 3 0 0 0 2.1-2.1A31.4 31.4 0 0 0 24 12a31.4 31.4 0 0 0-.5-5.8zM9.6 15.6V8.4l6.3 3.6-6.3 3.6z"/></svg>';
  } else {
    badge.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11.6 6.4h1.9v5.6h-1.9V6.4zm5.1 0h1.9v5.6h-1.9V6.4zM4.7 1L1 4.7v14.9h5.6V23l3.7-3.4h2.8L22 10.3V1H4.7zm15.4 8.5l-3.2 3.3h-3.8l-3.2 3.2v-3.2H5.7V2.9h14.4v6.6z"/></svg>';
  }
  return badge;
}

function resolveEmoteSrc(fragment) {
  if (fragment.emoteUrl && isSafeImageUrl(fragment.emoteUrl)) {
    return fragment.emoteUrl.trim();
  }
  if (fragment.emoteId) {
    return `https://static-cdn.jtvnw.net/emoticons/v2/${encodeURIComponent(String(fragment.emoteId))}/default/dark/3.0`;
  }
  return null;
}

// アバター用フォールバックカラーパレット
const AVATAR_PALETTE = [
  '#f97316', // オレンジ
  '#8b5cf6', // パープル
  '#10b981', // エメラルド
  '#06b6d4', // シアン
  '#ec4899', // ピンク
  '#3b82f6', // ブルー
  '#eab308', // イエロー
  '#ef4444', // レッド
  '#6366f1', // インディゴ
  '#14b8a6'  // ティール
];

function getAvatarColor(name, preferredColor) {
  if (preferredColor && /^#[0-9a-f]{6}$/i.test(preferredColor)) {
    return preferredColor;
  }
  if (!name) return '#6b7280';
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = (hash << 5) - hash + name.charCodeAt(i);
    hash |= 0;
  }
  return AVATAR_PALETTE[Math.abs(hash) % AVATAR_PALETTE.length];
}

function getAvatarInitial(name) {
  if (!name) return '?';
  const clean = String(name).replace(/^[@_\s]+/, '');
  return clean ? Array.from(clean)[0] : String(name).charAt(0);
}

// Twitch ユーザーのアバター画像キャッシュ (login -> url)
const twitchAvatarCache = new Map();

async function getTwitchUserAvatarUrl(userLogin) {
  if (!userLogin) return null;
  const login = String(userLogin).trim().toLowerCase().replace(/^@/, '');
  if (!login || login === 'anonymous' || login === 'user') return null;

  if (twitchAvatarCache.has(login)) {
    return twitchAvatarCache.get(login);
  }

  // 1. DecAPI による高速アバターURL取得 (CORS許可・OAuth不要・Twitch公式CDN URLを返却)
  try {
    const res = await fetch(`https://decapi.me/twitch/avatar/${encodeURIComponent(login)}`, {
      signal: AbortSignal.timeout(2200)
    });
    if (res.ok) {
      const url = (await res.text()).trim();
      if (url && /^https?:\/\//i.test(url) && !url.includes('User not found') && !url.includes('error')) {
        twitchAvatarCache.set(login, url);
        return url;
      }
    }
  } catch (e) {}

  return null;
}

function createUserAvatarElement(userName, userColor, avatarUrl, userLogin, platform) {
  const avatarEl = document.createElement('div');
  avatarEl.className = 'user-avatar';
  const bgColor = getAvatarColor(userName, userColor);
  avatarEl.style.backgroundColor = bgColor;

  function renderImage(src) {
    if (!src || !isSafeImageUrl(src)) return;
    avatarEl.textContent = '';
    const img = document.createElement('img');
    img.className = 'avatar-img';
    img.src = src;
    img.alt = userName;
    img.onerror = () => {
      img.remove();
      avatarEl.textContent = getAvatarInitial(userName);
    };
    avatarEl.appendChild(img);
  }

  if (avatarUrl && isSafeImageUrl(avatarUrl)) {
    renderImage(avatarUrl);
  } else {
    avatarEl.textContent = getAvatarInitial(userName);
    // Twitch の場合、ユーザーID/ログイン名から公式アバターアイコンを非同期取得して自動差し替え
    const login = userLogin || userName;
    if (login && platform !== 'youtube') {
      getTwitchUserAvatarUrl(login).then(url => {
        if (url) renderImage(url);
      }).catch(() => {});
    }
  }
  return avatarEl;
}

// コミック吹き出しの上部テール (名前ピル左端から左上のアバターへ向かって突き刺さる上左向きベクターSVG)
function createBubbleTailSvg() {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'bubble-tail');
  svg.setAttribute('viewBox', '0 0 28 14');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = '<path d="M 12,13 L 1,1 L 25,13 Z" fill="#000000" stroke="#ffffff" stroke-width="2" stroke-linejoin="round"/><line x1="12" y1="13" x2="25" y2="13" stroke="#000000" stroke-width="3"/>';
  return svg;
}

// チャットテキストおよび絵文字要素を body に展開する共通ヘルパー
function appendChatFragmentsToBody(body, japaneseResult, fragments, finalJapaneseText) {
  if (japaneseResult.isTranslated || fragments.length === 0) {
    if (finalJapaneseText) {
      const textSpan = document.createElement('span');
      textSpan.textContent = finalJapaneseText + ' ';
      body.appendChild(textSpan);
    }
    fragments.filter(f => f.type === 'emote').forEach(f => {
      const src = resolveEmoteSrc(f);
      if (!src) return;
      const img = document.createElement('img');
      img.className = 'chat-emote';
      img.src = src;
      img.alt = f.text || 'emote';
      body.appendChild(img);
    });
  } else {
    fragments.forEach(f => {
      if (f.type === 'emote') {
        const src = resolveEmoteSrc(f);
        if (!src) return;
        const img = document.createElement('img');
        img.className = 'chat-emote';
        img.src = src;
        img.alt = f.text || 'emote';
        body.appendChild(img);
      } else if (f.text) {
        const span = document.createElement('span');
        span.textContent = f.text;
        body.appendChild(span);
      }
    });
  }
}

// 非同期翻訳待ちによるコメント表示順の逆転を防ぐ直列処理チェーン
let chatProcessingChain = Promise.resolve();

function handleIncomingChat(chatPayload) {
  chatProcessingChain = chatProcessingChain
    .then(() => processIncomingChatInternal(chatPayload))
    .catch((err) => console.warn('handleIncomingChat error:', err));
  return chatProcessingChain;
}

async function processIncomingChatInternal({
  platform = 'twitch',
  messageId,
  userLogin,
  userName,
  userColor,
  avatarUrl,
  badges = [],
  fragments = [],
  rawText = ''
}) {
  if (messageId && markProcessed(messageId)) return;

  // Twitch でアバター未取得なら事前フェッチ（キャッシュがあれば即座にURLが入る）
  if (platform === 'twitch' && !avatarUrl) {
    const login = userLogin || userName;
    if (login) {
      try {
        avatarUrl = await getTwitchUserAvatarUrl(login);
      } catch (e) {}
    }
  }

  // 1. テキスト部分を抽出（スタンプ以外のテキスト）
  const extractedText = fragments
    .filter(f => f.type === 'text')
    .map(f => f.text)
    .join(' ')
    .trim() || (rawText || '').trim();

  // 2. 先に日本語へ変換する（外国語の場合はここで日本語テキストに置き換わる）
  const japaneseResult = extractedText
    ? await convertCommentToJapanese(extractedText)
    : { text: '', isTranslated: false, lang: 'JA' };

  const finalJapaneseText = japaneseResult.text;

  // 履歴への記録 (UC-09): 表示が確定したコメントを原文のまま送る
  if (window.FeedLog) {
    FeedLog.record({
      kind: 'comment',
      id: messageId || undefined,
      source: platform,
      user: userName || '',
      userLogin: userLogin || '',
      text: extractedText
    });
  }

  // 3A. 日本語変換後のテキストを使ってビュワーのカードDOMを生成・表示
  const card = document.createElement('div');
  card.className = 'feed-item comment-card';

  if (settings.theme === 'comic') {
    // ========================================================
    // コミック吹き出しスタイル（スト6風）
    // 1行目: [アバター] [名前ピル]
    // 2行目: [コメント吹き出し (上部テールがアバターに刺さる)]
    // ========================================================
    if (userName) {
      const header = document.createElement('div');
      header.className = 'comment-header';

      header.appendChild(createUserAvatarElement(userName, userColor, avatarUrl, userLogin, platform));

      const pill = document.createElement('div');
      pill.className = 'author-pill';

      pill.appendChild(createPlatformIconBadge(platform));

      badges.forEach(b => {
        const span = document.createElement('span');
        span.className = `role-badge ${b.cls}`;
        span.textContent = b.label;
        pill.appendChild(span);
      });

      const nameEl = document.createElement('span');
      nameEl.className = 'user-name';
      nameEl.style.color = userColor || '#a970ff';
      nameEl.textContent = (userName && !userName.startsWith('@')) ? `@${userName}` : userName;
      pill.appendChild(nameEl);

      if (japaneseResult.isTranslated) {
        const langBadge = document.createElement('span');
        langBadge.className = 'lang-badge';
        langBadge.textContent = `🌐 ${japaneseResult.lang}→JA`;
        pill.appendChild(langBadge);
        notifyForeignCommentToJimakuChan(japaneseResult.lang);
      }

      header.appendChild(pill);
      card.appendChild(header);
    }

    const bubble = document.createElement('div');
    bubble.className = 'comment-bubble';
    if (!userName) {
      bubble.classList.add('no-header');
    }

    const body = document.createElement('div');
    body.className = 'comment-body';
    appendChatFragmentsToBody(body, japaneseResult, fragments, finalJapaneseText);

    bubble.appendChild(body);
    if (userName) {
      bubble.appendChild(createBubbleTailSvg());
    }
    card.appendChild(bubble);

  } else {
    // ========================================================
    // 元のスタンダード（ダークグラス）スタイル（100%完全再現）
    // ========================================================
    const header = document.createElement('div');
    header.className = 'comment-header';

    header.appendChild(createPlatformIconBadge(platform));

    badges.forEach(b => {
      const span = document.createElement('span');
      span.className = `role-badge ${b.cls}`;
      span.textContent = b.label;
      header.appendChild(span);
    });

    const nameEl = document.createElement('span');
    nameEl.className = 'user-name';
    nameEl.style.color = userColor || '#a970ff';
    nameEl.textContent = userName || '匿名ユーザー';
    header.appendChild(nameEl);

    if (japaneseResult.isTranslated) {
      const langBadge = document.createElement('span');
      langBadge.className = 'lang-badge';
      langBadge.textContent = `🌐 ${japaneseResult.lang}→JA`;
      header.appendChild(langBadge);
      notifyForeignCommentToJimakuChan(japaneseResult.lang);
    }

    const body = document.createElement('div');
    body.className = 'comment-body';
    appendChatFragmentsToBody(body, japaneseResult, fragments, finalJapaneseText);

    card.appendChild(header);
    card.appendChild(body);
  }

  appendFeedElement(card);

  // 3B. 変換済みの日本語テキストを VOICEVOX 読み上げへ流す
  if (finalJapaneseText) {
    const spokenName = sanitizeUsernameForSpeech(userName);
    const speech = settings.read_username
      ? `${spokenName}さん。${finalJapaneseText}`
      : finalJapaneseText;
    enqueueTts(speech);
  }
}

/* ========================================================
 * 各種アラート処理本体（userInput / messageText 両対応）
 * ======================================================== */
async function handleIncomingAlert({
  platform = 'twitch',
  alertId,
  kind,       // 'redemption' | 'follow' | 'sub' | 'cheer' | 'raid' | 'milestone'
  typeLabel,
  titleText,
  userInput = '',
  messageText = '',
  ttsText = '',
  userName = '',
  userLogin = '',
  userColor = '',
  avatarUrl = '',
  badges = []
}) {
  if (alertId && markProcessed(alertId)) return;

  // 履歴への記録 (UC-09): 表示が確定したアラートを送る
  if (window.FeedLog) {
    FeedLog.record({
      kind: 'alert',
      id: alertId || undefined,
      source: platform,
      alertKind: kind,
      label: typeLabel || '',
      user: userName || '',
      userLogin: userLogin || '',
      title: titleText || '',
      text: (userInput || messageText || '').trim()
    });
  }

  const rawAlertMessage = (userInput || messageText || '').trim();
  let finalInputJa = rawAlertMessage;
  if (rawAlertMessage) {
    const trans = await convertCommentToJapanese(rawAlertMessage);
    finalInputJa = trans.text;
  }

  // ユーザー名の抽出フォールバック
  let effectiveUserName = userName;
  if (!effectiveUserName && titleText) {
    const m = titleText.match(/^(.+?)\s*さんが/);
    if (m) effectiveUserName = m[1].trim();
  }

  // Twitch アバターの遅延取得
  if (platform === 'twitch' && !avatarUrl) {
    const login = userLogin || effectiveUserName;
    if (login) {
      try {
        avatarUrl = await getTwitchUserAvatarUrl(login);
      } catch (e) {}
    }
  }

  const card = document.createElement('div');

  if (settings.theme === 'comic') {
    // ========================================================
    // コミック吹き出しスタイル（スト6風）
    // 1行目: [アバター] [名前ピル (プラットフォーム + 種別バッジ + ユーザー名)]
    // 2行目: [コメント吹き出し (上部テールがアバターに刺さる)]
    // ========================================================
    card.className = `feed-item comment-card alert-card-comic alert-${kind}`;

    if (effectiveUserName) {
      const header = document.createElement('div');
      header.className = 'comment-header';

      header.appendChild(createUserAvatarElement(effectiveUserName, userColor, avatarUrl, userLogin, platform));

      const pill = document.createElement('div');
      pill.className = 'author-pill';

      pill.appendChild(createPlatformIconBadge(platform));

      const alertBadge = document.createElement('span');
      alertBadge.className = `role-badge alert-badge-${kind}`;
      alertBadge.textContent = typeLabel;
      pill.appendChild(alertBadge);

      badges.forEach(b => {
        const span = document.createElement('span');
        span.className = `role-badge ${b.cls}`;
        span.textContent = b.label;
        pill.appendChild(span);
      });

      const nameEl = document.createElement('span');
      nameEl.className = 'user-name';
      nameEl.style.color = userColor || (kind === 'cheer' ? '#f59e0b' : (kind === 'redemption' ? '#10b981' : (kind === 'milestone' ? '#f97316' : '#a970ff')));
      nameEl.textContent = (effectiveUserName && !effectiveUserName.startsWith('@')) ? `@${effectiveUserName}` : effectiveUserName;
      pill.appendChild(nameEl);

      header.appendChild(pill);
      card.appendChild(header);
    }

    const bubble = document.createElement('div');
    bubble.className = `comment-bubble alert-bubble alert-bubble-${kind}`;
    if (!effectiveUserName) {
      bubble.classList.add('no-header');
    }

    const alertActionLine = document.createElement('div');
    alertActionLine.className = 'alert-action-line';
    alertActionLine.textContent = titleText;
    bubble.appendChild(alertActionLine);

    if (finalInputJa) {
      const body = document.createElement('div');
      body.className = 'comment-body alert-comment-body';
      body.textContent = finalInputJa;
      bubble.appendChild(body);
    }

    if (effectiveUserName) {
      bubble.appendChild(createBubbleTailSvg());
    }
    card.appendChild(bubble);

  } else {
    // ========================================================
    // 元のスタンダード（ダークグラス）スタイル
    // ========================================================
    card.className = `feed-item alert-card alert-${kind}`;

    const top = document.createElement('div');
    top.className = 'alert-top';

    const pill = document.createElement('span');
    pill.className = 'alert-type-label';
    pill.appendChild(createPlatformIconBadge(platform));
    const pillText = document.createElement('span');
    pillText.textContent = typeLabel;
    pill.appendChild(pillText);
    top.appendChild(pill);

    const title = document.createElement('div');
    title.className = 'alert-title';
    title.textContent = titleText;

    card.appendChild(top);
    card.appendChild(title);

    if (finalInputJa) {
      const msg = document.createElement('div');
      msg.className = 'alert-message';
      msg.textContent = finalInputJa;
      card.appendChild(msg);
    }
  }

  appendFeedElement(card);

  if (settings.read_alerts) {
    const speech = ttsText || (finalInputJa ? `${titleText}。${finalInputJa}` : titleText);
    enqueueTts(speech);
  }
}

/* ========================================================
 * テスト発火機能（生パケットの重複受信→重複排除パイプラインを一貫検証）
 * ======================================================== */
async function runTestTrigger(kind) {
  // 0. サンプル一括表示テスト（架空のサンプルコメント7件を描画）
  if (kind === 'sf6-demo') {
    const samples = [
      {
        userName: '',
        userColor: '#ffffff',
        text: 'サンプルコメント（名前なし）'
      },
      {
        userName: 'サンプルリスナー1',
        userColor: '#f97316',
        text: 'がんばれー！'
      },
      {
        userName: 'サンプルリスナー2',
        userColor: '#8b5cf6',
        text: 'いまのコンボすごかった'
      },
      {
        userName: 'サンプルリスナー3',
        userColor: '#eab308',
        text: '初見です、よろしくお願いします'
      },
      {
        userName: 'サンプルリスナー4',
        userColor: '#38bdf8',
        text: 'その技どうやって出すんですか？'
      },
      {
        userName: 'サンプルリスナー5',
        userColor: '#f43f5e',
        text: 'ナイスファイトでした'
      },
      {
        userName: 'サンプルリスナー6',
        userColor: '#10b981',
        text: '次も見に来ます'
      }
    ];

    setLog('⚡ サンプルコメント例（7件）を順番にテスト描画中...');
    samples.forEach((s, idx) => {
      setTimeout(() => {
        handleIncomingChat({
          platform: 'youtube',
          messageId: `sf6-demo-${idx}-${Date.now()}`,
          userName: s.userName,
          userColor: s.userColor,
          avatarUrl: s.avatarUrl || '',
          badges: s.badges || [],
          fragments: [{ type: 'text', text: s.text }]
        });
      }, idx * 320);
    });
    return;
  }

  // 1. YouTube 系イベント: server.py 側の InnerTube パーサー (_ingest_actions) に3重重複アクションを注入し、
  //    実際の /api/youtube/poll ポーリング経由で CommentViewer に拾わせる（サーバー未起動時のみローカルフォールバック）
  if (kind.startsWith('yt-')) {
    try {
      const res = await authFetch(`${API_BASE}/api/youtube/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind })
      });
      if (res.ok) {
        const info = await res.json();
        setLog(`🔴 YTテスト発火 (${kind}): 生アクション${info.simulated_raw_count}件注入 → 重複${info.duplicates_blocked}件ブロック`);
        if (typeof pollYouTubeLiveChat === 'function') {
          await pollYouTubeLiveChat();
        }
        return;
      }
    } catch (e) {}

    // サーバー未起動時のフォールバック（同一IDで3回呼び出してクライアント側の markProcessed 重複排除を検証）
    const dupId = `test-${kind}-${Date.now()}`;
    for (let i = 0; i < 3; i++) {
      if (kind === 'yt-jp-chat') {
        handleIncomingChat({
          platform: 'youtube',
          messageId: dupId,
          userName: 'サンプルリスナーYT',
          userColor: '#ff4e45',
          avatarUrl: 'https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?auto=format&fit=crop&w=80&q=80',
          badges: [{ label: 'MEMBER', cls: 'badge-sub' }],
          fragments: [{ type: 'text', text: 'YouTubeから観てます！高画質で鍵盤もスプラも見やすいです！' }]
        });
      } else if (kind === 'yt-en-chat') {
        handleIncomingChat({
          platform: 'youtube',
          messageId: dupId,
          userName: 'Alex_From_YouTube',
          userColor: '#ff4e45',
          avatarUrl: 'https://images.unsplash.com/photo-1570295999919-56ceb5ecca61?auto=format&fit=crop&w=80&q=80',
          badges: [],
          fragments: [{ type: 'text', text: 'Watching from YouTube Live! Your stream setup is awesome!' }]
        });
      } else if (kind === 'yt-superchat') {
        handleIncomingAlert({
          platform: 'youtube',
          alertId: dupId,
          kind: 'cheer',
          typeLabel: '💴 SUPER CHAT (￥1,000)',
          titleText: 'YouTubeサポーター さんがスパチャを送りました！ ￥1,000',
          messageText: 'いつも楽しい配信ありがとうございます！応援してます！',
          ttsText: 'YouTubeで、YouTubeサポーターさんが、1000円のスーパーチャットを送りました！いつも楽しい配信ありがとうございます！応援してます！'
        });
      } else if (kind === 'yt-member') {
        handleIncomingAlert({
          platform: 'youtube',
          alertId: dupId,
          kind: 'sub',
          typeLabel: '⭐ YOUTUBE MEMBER',
          titleText: 'サンプルメンバーYT さん (新規メンバーシップ加入)',
          messageText: 'YouTubeメンバーシップ入りました！スタンプ使うの楽しみです！',
          ttsText: 'YouTubeで、サンプルメンバーYTさんがメンバーシップに加入しました！YouTubeメンバーシップ入りました！スタンプ使うの楽しみです！'
        });
      }
    }
    return;
  }

  // 2. Twitch 系イベント: IRC 生行パーサー (processRawIrcData) と EventSub ハンドラ (handleEventSubNotification) の
  //    両方に本番さながらの重複パケット群を同時注入して、1枚だけに制御されることを検証
  if (typeof simulateTwitchEventPacketBurst === 'function') {
    simulateTwitchEventPacketBurst(kind);
    setLog(`🟣 TW重複耐性テスト発火 (${kind}): IRC＋EventSub複数パケット同時注入 → 1枚に統合`);
  }
}

document.querySelectorAll('[data-test]').forEach(btn => {
  btn.addEventListener('click', () => runTestTrigger(btn.getAttribute('data-test')));
});
btnClearFeed.addEventListener('click', () => { stage.innerHTML = ''; });
