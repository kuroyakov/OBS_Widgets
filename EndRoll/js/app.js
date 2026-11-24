const CONFIG = {
  title: '',
  // 配信者名（クレジット表示用）。空のままなら行ごと表示しない
  streamer: '',
  role: 'STREAMER',
  closingMessage: 'THANK YOU FOR WATCHING!',
  speed: 75,
  font: 'mincho',
  fontSize: 'medium',
  showCommentCount: false,

  // 直近何時間以内のコメントを対象にするか（1回目の混入防止）
  // 0 にすると全期間対象
  targetHours: 4
};

;(() => {
  'use strict';

  const track = document.getElementById('credits-track');
  const statusAlert = document.getElementById('status-alert');

  let currentAnimation = null;
  let rawCommentsList = [];
  let rawNotificationsList = [];
  let isDataReceived = false;
  // 停止位置の測定世代と、最後に測った closing 中心（Issue #140 の補正用）
  let animationSeq = 0;
  let lastClosingCenter = null;
  let closingWatchTimer = null;

  function str (val) {
    return val === null || val === undefined ? '' : String(val).trim();
  }

  // わんコメ OneSDK の meta（配信情報）から配信タイトルを反映する（Issue #104）
  function applyMetaTitle (payload) {
    const liveTitle = str(payload?.data?.title || '');
    if (!liveTitle || liveTitle === CONFIG.title) return;
    CONFIG.title = liveTitle;
    const el = document.getElementById('ui-title');
    if (el) {
      el.textContent = liveTitle;
      el.hidden = false;
    }
    // タイトル差し替えでレイアウトが伸びたら、再生中はなめらかに進路を直し、
    // 終了後は停止位置を補正する（Issue #140）
    if (currentAnimation && currentAnimation.playState === 'running') {
      retargetClosingIfNeeded();
    } else if (currentAnimation && currentAnimation.playState === 'finished') {
      correctClosingPosition();
    }
  }

  // レイアウト確定待ち（Issue #140）: Webフォントの読み込みが終わるまで測らない。
  // 再生開始が遅れすぎないよう、待ちには上限を設ける
  function waitForFontsReady () {
    if (!document.fonts || !document.fonts.ready) return Promise.resolve();
    return Promise.race([
      document.fonts.ready,
      new Promise(resolve => setTimeout(resolve, 1000))
    ]).catch(() => {});
  }

  // 停止位置の再測定と補正（Issue #140）: 測定後にタイトル差し込みやフォント
  // 差し替えでレイアウトが伸びていたら、closing が画面中央に来るよう最終位置を直す。
  // アニメーションの fill がインライン指定より優先されるため、補正後に cancel する
  function correctClosingPosition () {
    if (lastClosingCenter === null) return;
    const closingEl = document.getElementById('ui-closing');
    if (!closingEl) return;
    const nowCenter = closingEl.offsetTop + (closingEl.offsetHeight / 2);
    if (Math.abs(nowCenter - lastClosingCenter) <= 2) return;
    lastClosingCenter = nowCenter;
    const targetY = (window.innerHeight / 2) - nowCenter;
    track.style.transform = `translate(-50%, ${targetY}px)`;
    if (currentAnimation) {
      currentAnimation.cancel();
      currentAnimation = null;
    }
  }

  // トラックの現在位置（アニメーション適用後の transform から読む。Issue #140）
  function currentTrackY () {
    try {
      const t = window.getComputedStyle(track).transform;
      if (!t || t === 'none') return null;
      return new DOMMatrixReadOnly(t).m42;
    } catch (e) {
      return null;
    }
  }

  // 再生中の進路修正（Issue #140）: レイアウトが伸びていたら、現在位置から
  // 新しい停止位置へ同じ速度でアニメーションを張り直す。終了時に一括で直すと
  // 画面上で飛んで見えるため、伸びた時点で（closing が画面外にある間に）直す
  function retargetClosingIfNeeded () {
    if (!currentAnimation || currentAnimation.playState !== 'running') return;
    if (lastClosingCenter === null) return;
    const closingEl = document.getElementById('ui-closing');
    if (!closingEl) return;
    const nowCenter = closingEl.offsetTop + (closingEl.offsetHeight / 2);
    if (Math.abs(nowCenter - lastClosingCenter) <= 2) return;
    const currentY = currentTrackY();
    if (currentY === null) return; // 位置が読めない回は見送り、見張りが再試行する
    lastClosingCenter = nowCenter;
    const targetY = (window.innerHeight / 2) - nowCenter;
    const pixelsPerSecond = Math.max(20, CONFIG.speed || 75);
    const duration = Math.max(300, (Math.abs(targetY - currentY) / pixelsPerSecond) * 1000);
    currentAnimation.cancel();
    currentAnimation = track.animate([
      { transform: `translate(-50%, ${currentY}px)` },
      { transform: `translate(-50%, ${targetY}px)` }
    ], {
      duration,
      easing: 'linear',
      fill: 'forwards'
    });
    currentAnimation.onfinish = handleAnimationFinish;
  }

  // 再生中はレイアウトの伸びを定期的に見張る（伸びの要因を問わないための受け皿）
  function startClosingWatch () {
    stopClosingWatch();
    closingWatchTimer = setInterval(retargetClosingIfNeeded, 500);
  }

  function stopClosingWatch () {
    if (closingWatchTimer !== null) {
      clearInterval(closingWatchTimer);
      closingWatchTimer = null;
    }
  }

  function handleAnimationFinish () {
    stopClosingWatch();
    correctClosingPosition();
  }

  function applyConfig () {
    const titleEl = document.getElementById('ui-title');
    titleEl.textContent = CONFIG.title;
    titleEl.hidden = !CONFIG.title;

    document.getElementById('ui-streamer').textContent = CONFIG.streamer;
    document.getElementById('ui-streamer').hidden = !CONFIG.streamer;

    document.getElementById('ui-role').textContent = CONFIG.role;
    document.getElementById('ui-role').hidden = !CONFIG.role || !CONFIG.streamer;

    document.getElementById('ui-closing').textContent = CONFIG.closingMessage;

    const fontMap = {
      mincho: '"Shippori Mincho", "Noto Serif JP", serif',
      gothic: '"Noto Sans JP", sans-serif',
      rounded: '"M PLUS Rounded 1c", "Noto Sans JP", sans-serif',
      cinzel: '"Cinzel", "Shippori Mincho", serif'
    };
    document.documentElement.style.setProperty('--font-family', fontMap[CONFIG.font] || fontMap.mincho);
    document.documentElement.style.setProperty('--roll-speed', `${Math.max(20, CONFIG.speed || 75)}px`);

    if (CONFIG.fontSize === 'small') document.body.classList.add('size-small');
    if (CONFIG.fontSize === 'large') document.body.classList.add('size-large');
  }

  // タイムスタンプをミリ秒へそろえる。秒単位の epoch（10桁）が混ざっても
  // ミリ秒として誤読して「古すぎる」と捨てないための変換（Issue #102）
  function toMillis (timestamp) {
    if (timestamp === null || timestamp === undefined || timestamp === '') return NaN;
    if (typeof timestamp === 'number') {
      return timestamp < 1e12 ? timestamp * 1000 : timestamp;
    }
    return new Date(timestamp).getTime();
  }

  function isWithinTargetHours(timestamp) {
    if (!CONFIG.targetHours || CONFIG.targetHours <= 0) return true;
    if (!timestamp) return true;

    const time = toMillis(timestamp);
    if (isNaN(time)) return true;

    const cutoffTime = Date.now() - (CONFIG.targetHours * 60 * 60 * 1000);
    return time >= cutoffTime;
  }

  function buildCreditsData() {
    const state = {
      participants: new Map(),
      follows: new Set(),
      subscriptions: new Set(),
      subgifts: new Set(),
      milestones: new Set(),
      channelpoints: new Set(),
      bits: new Set(),
      processedEvents: new Set(),
      // 集計の内訳診断（Issue #102）: 空になったときに未着かフィルタ落ちかを判別する
      diag: { noData: 0, outOfWindow: 0, noName: 0, counted: 0 }
    };

    function recordParticipant (name, userId) {
      if (!name) return;
      const key = userId || name;
      const existing = state.participants.get(key) || { name, count: 0, firstOrder: state.participants.size };
      existing.count += 1;
      state.participants.set(key, existing);
    }

    function processItem(comment) {
      if (!comment?.data) { state.diag.noData += 1; return; }

      const itemTime = comment.data.timestamp || comment.data.createdAt || comment.timestamp;
      if (!isWithinTargetHours(itemTime)) { state.diag.outOfWindow += 1; return; }

      const data = comment.data;
      const name = str(data.displayName || data.name || data.username || data.author || comment.name || comment.author || '');
      if (!name) { state.diag.noName += 1; return; }

      const userId = str(data.userId || data.channelId || data.authorId || '');
      const text = str(data.comment || data.message || '').slice(0, 500);
      const giftType = str(data.giftType || data.type).toLowerCase();

      const eventId = str(data.id || comment.id || data.uuid);
      const eventKey = `${eventId || `${name}_${text}_${giftType}_${data.timestamp || ''}`}`;
      if (state.processedEvents.has(eventKey)) return;
      state.processedEvents.add(eventKey);

      state.diag.counted += 1;
      recordParticipant(name, userId);

      if (giftType === 'follow' || /フォロー(?:しました|されました)|(?:new\s+)?follow(?:ed)?\b/i.test(text)) {
        state.follows.add(name);
        return;
      }

      const isPoint = data.reward !== undefined || data.redemption !== undefined || data.customRewardId !== undefined ||
        giftType.includes('channelpoint') || giftType.includes('reward') ||
        /チャンネルポイント|引き換え(?:まし|完了)|redeemed\b/i.test(text);
      if (isPoint) {
        state.channelpoints.add(name);
        return;
      }

      const isSubGift = giftType.includes('subgift') ||
        (data.gift && (data.gift.gifterDisplayName || data.gift.gifterName || data.gift.count !== undefined)) ||
        /サブスク(?:.*?)贈りました|サブスク(?:.*?)プレゼント|gifted.*?sub/i.test(text);
      if (isSubGift) {
        const gifter = str((data.gift && (data.gift.gifterDisplayName || data.gift.gifterName)) || name);
        state.subgifts.add(gifter);
        return;
      }

      const isMilestone = giftType.includes('resub') || giftType.includes('milestone') ||
        /通算\s*\d+\s*(?:か月|ヶ月|ヵ月|カ月)|連続\s*\d+\s*(?:か月|ヶ月|ヵ月|カ月)|resubscribed|連続視聴回数/i.test(text);
      if (isMilestone) {
        state.milestones.add(name);
        return;
      }

      const isSub = giftType.includes('sub') || /サブスクしました|subscribed with\b/i.test(text) || (data.isSubscriber && data.hasGift);
      if (isSub) {
        state.subscriptions.add(name);
        return;
      }

      const isBits = (data.bits && Number(data.bits) > 0) ||
        (data.gift && (data.gift.bits || /bits|cheer/i.test(str(data.gift.type)))) ||
        giftType === 'bits' || giftType === 'cheer' ||
        data.hasGift || /cheer\d+\b|ビッツ/i.test(text);
      if (isBits) {
        state.bits.add(name);
      }
    }

    rawCommentsList.forEach(processItem);

    rawNotificationsList.forEach(item => {
      if (!item) return;
      const data = item.data || item;
      let name = str(data.displayName || data.name || data.username || data.author || item.displayName || item.name || item.user || '');
      const text = str(data.comment || data.message || item.message || '').slice(0, 500);

      if (!name && text) {
        const match = text.match(/^(.+?)(?:さん)?が(?:フォロー|サブスク|チャンネルポイント)/) ||
                      text.match(/^(.+?)\s+(?:followed|subscribed|cheered|redeemed)/i);
        if (match && match.at(1)) name = match.at(1).trim();
      }
      if (!name) return;

      processItem({
        service: 'twitch',
        data: {
          id: data.id || item.id || data.uuid || `${name}_${Date.now()}`,
          name,
          comment: text,
          giftType: data.giftType || item.type || '',
          ...data
        }
      });
    });

    return state;
  }

  function createRow (name, count) {
    const row = document.createElement('div');
    row.className = 'credit-item';
    const nameSpan = document.createElement('span');
    nameSpan.textContent = name;
    row.appendChild(nameSpan);

    if (CONFIG.showCommentCount && count !== undefined) {
      const countSpan = document.createElement('span');
      countSpan.className = 'item-count';
      countSpan.textContent = `(${count})`;
      row.appendChild(countSpan);
    }
    return row;
  }

  function renderSection (sectionId, listId, setOrMap, isParticipantMap = false) {
    const section = document.getElementById(sectionId);
    const container = document.getElementById(listId);
    if (!section || !container) return;

    let rows = [];
    if (isParticipantMap) {
      const sorted = Array.from(setOrMap.values()).sort((a, b) =>
        b.count - a.count || a.firstOrder - b.firstOrder || a.name.localeCompare(b.name, 'ja')
      );
      rows = sorted.map(p => createRow(p.name, p.count));
    } else {
      rows = Array.from(setOrMap).map(name => createRow(name));
    }

    container.replaceChildren(...rows);
    section.hidden = rows.length === 0;
  }

  function renderAll (state) {
    renderSection('sec-comments', 'list-comments', state.participants, true);
    renderSection('sec-follows', 'list-follows', state.follows);
    renderSection('sec-subscriptions', 'list-subscriptions', state.subscriptions);
    renderSection('sec-subgifts', 'list-subgifts', state.subgifts);
    renderSection('sec-milestones', 'list-milestones', state.milestones);
    renderSection('sec-channelpoints', 'list-channelpoints', state.channelpoints);
    renderSection('sec-bits', 'list-bits', state.bits);
  }

  /* ========================================================
   * アニメーション開始・再スタート処理
   * ======================================================== */
  async function startAnimation () {
    const seq = ++animationSeq;
    // 既存アニメーションを完全停止・リセット
    stopClosingWatch();
    if (currentAnimation) {
      currentAnimation.cancel();
      currentAnimation = null;
    }

    // 最新のデータで集計・再描画
    const state = buildCreditsData();
    renderAll(state);

    // 集計の内訳診断（Issue #102）: 空になったときに「未着」か「フィルタ落ち」かを
    // コンソールと画面表示で判別できるようにする
    const serviceCounts = {};
    rawCommentsList.forEach(c => {
      const s = (c && c.service) || 'unknown';
      serviceCounts[s] = (serviceCounts[s] || 0) + 1;
    });
    console.log('[EndRoll] 集計内訳', {
      comments: rawCommentsList.length,
      notifications: rawNotificationsList.length,
      services: serviceCounts,
      participants: state.participants.size,
      noData: state.diag.noData,
      outOfWindow: state.diag.outOfWindow,
      noName: state.diag.noName,
      counted: state.diag.counted
    });
    if (window.OneSDK && rawCommentsList.length === 0 && rawNotificationsList.length === 0) {
      statusAlert.textContent = 'わんコメからコメントが届いていません（集計なしで再生）';
      statusAlert.hidden = false;
    } else if (state.participants.size === 0 && (rawCommentsList.length > 0 || rawNotificationsList.length > 0)) {
      statusAlert.textContent = 'コメントは受信しましたが、集計対象がありませんでした';
      statusAlert.hidden = false;
    } else if (window.OneSDK) {
      statusAlert.hidden = true;
    }

    // トラックを表示状態にして初期位置（画面下端外）へ
    track.classList.add('active');
    track.style.transform = `translate(-50%, ${window.innerHeight + 20}px)`;

    // 測る前にフォントの確定を待つ（Issue #140）。待つ間に再スタートされたら中断する
    await waitForFontsReady();
    if (seq !== animationSeq) return;

    // 次の描画フレームでスクロールアニメーションを開始
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (seq !== animationSeq) return;
        const closingEl = document.getElementById('ui-closing');
        const startY = window.innerHeight + 20;
        const closingCenter = closingEl.offsetTop + (closingEl.offsetHeight / 2);
        lastClosingCenter = closingCenter;
        const targetY = (window.innerHeight / 2) - closingCenter;
        const totalDistance = Math.max(1, startY - targetY);

        const pixelsPerSecond = Math.max(20, CONFIG.speed || 75);
        const duration = Math.max(8000, (totalDistance / pixelsPerSecond) * 1000);

        currentAnimation = track.animate([
          { transform: `translate(-50%, ${startY}px)` },
          { transform: `translate(-50%, ${targetY}px)` }
        ], {
          duration,
          easing: 'linear',
          fill: 'forwards'
        });
        // 終了時点でレイアウトが伸びていたら停止位置を補正する（Issue #140）。
        // 再生中も見張り、伸びたら closing が画面に出る前に進路を直す
        currentAnimation.onfinish = handleAnimationFinish;
        startClosingWatch();
      });
    });
  }

  // ★ 1. OBSのシーン切り替えイベント（アクティブ化）を直接検知 ★
  window.addEventListener('obsSourceActiveChanged', (event) => {
    if (event.detail.active) {
      // シーンが画面に表示されたら再再生
      setTimeout(startAnimation, 300);
    }
  });

  // ★ 2. ブラウザ標準の表示切替イベント ★
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      setTimeout(startAnimation, 300);
    }
  });

  // ★ 3. 手動テスト・やり直し（Rキー または 画面クリック）★
  window.addEventListener('keydown', (e) => {
    if (e.key === 'r' || e.key === 'R') {
      startAnimation();
    }
  });
  window.addEventListener('click', () => {
    startAnimation();
  });

  applyConfig();

  if (!window.OneSDK) {
    statusAlert.textContent = 'OneSDK未検出（プレビュー再生）';
    statusAlert.hidden = false;
    setTimeout(startAnimation, 800);
    return;
  }

  OneSDK.ready().then(() => {
    let perms = undefined;
    if (OneSDK.PERM) {
      const desired = [OneSDK.PERM.COMMENT];
      if (OneSDK.PERM.META) desired.push(OneSDK.PERM.META);
      if (OneSDK.PERM.NOTIFICATION) desired.push(OneSDK.PERM.NOTIFICATION);
      if (OneSDK.PERM.SYSTEM_COMMENT) desired.push(OneSDK.PERM.SYSTEM_COMMENT);
      if (typeof OneSDK.usePermission === 'function') {
        perms = OneSDK.usePermission(desired);
      } else {
        perms = desired;
      }
    }

    OneSDK.setup({
      permissions: perms,
      commentLimit: 10000,
      disabledDelay: true
    });

    OneSDK.subscribe({
      action: 'comments',
      callback: (payload) => {
        const list = Array.isArray(payload)
          ? payload
          : (payload?.comments || payload?.data?.comments || []);
        if (Array.isArray(list)) {
          rawCommentsList = list;
          // 初回データ受信時のスタート
          if (!isDataReceived) {
            isDataReceived = true;
            setTimeout(startAnimation, 400);
          }
        }
      }
    });

    OneSDK.subscribe({
      action: 'notification',
      callback: (payload) => {
        const list = Array.isArray(payload)
          ? payload
          : (payload?.notifications || payload?.data || [payload]);
        if (Array.isArray(list)) {
          rawNotificationsList = list;
        }
      }
    });

    // 配信情報（タイトル）の反映。複数サービス接続時は最後に届いたものを採用する
    OneSDK.subscribe({
      action: 'meta',
      callback: (payload) => {
        applyMetaTitle(payload);
      }
    });

    OneSDK.connect();

    // 接続フォールバック（コメント受信がなくても3.5秒後に開始）
    setTimeout(() => {
      if (!isDataReceived) {
        startAnimation();
      }
    }, 3500);
  }).catch(err => {
    console.error(err);
    statusAlert.textContent = 'わんコメ接続失敗（集計なしで再生）';
    statusAlert.hidden = false;
    setTimeout(startAnimation, 1000);
  });
})();
