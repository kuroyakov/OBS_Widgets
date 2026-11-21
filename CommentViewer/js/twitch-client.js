// ========================================================
// Twitch IRC WebSocket ＆ EventSub WebSocket 接続・初期化 (js/twitch-client.js)
// ========================================================
'use strict';

// ビッツメッセージ内の Cheermote トークン (例: "Cheer100 ", "cheer500 ") を除去して純粋なコメント本文を取り出す
function stripCheermoteTokens(rawMessage) {
  if (!rawMessage) return '';
  const cleaned = String(rawMessage)
    .replace(/\b(?:cheer|anoncheer|biblethump|cheerwhal|corgo|uni|showlove|party|seemsgood|pride|kappa|frankerz|heyguys|dansgame|elegiggle|trihard|kreygasm|4head|swiftrage|notlikethis|failfish|vohiyo|pjsalt|mrdestructoid|bday|ripcheer|shamrock)\d+\b/gi, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
  return cleaned;
}

/* ========================================================
 * IRC / EventSub 間の横断的な重複排除・前後到着吸収ガード
 * ======================================================== */
// 1) 同一の意味を持つアラート（IRC USERNOTICE と EventSub notification で別IDとして届くもの）を時間窓で重複排除
const recentAlertSignatures = new Map();
function isDuplicateAlertSignature(sigKey, windowMs = 8000) {
  if (!sigKey) return false;
  const now = Date.now();
  for (const [k, ts] of recentAlertSignatures.entries()) {
    if (now - ts > windowMs) recentAlertSignatures.delete(k);
  }
  const prevTs = recentAlertSignatures.get(sigKey);
  if (prevTs && (now - prevTs) <= windowMs) {
    return true;
  }
  recentAlertSignatures.set(sigKey, now);
  return false;
}

// 2) チャネポ・ビッツ・サブスク継続メッセージ等で、アラート側が先に届いて後から通常チャット(channel.chat.message)が届いた場合もブロック
const recentSuppressedChatKeys = new Map();
function buildChatMatchKeys(userName, rawMessageText) {
  if (!userName || !rawMessageText) return [];
  const u = String(userName).trim().toLowerCase();
  const trimmed = String(rawMessageText).trim();
  const stripped = stripCheermoteTokens(trimmed);
  const keys = [`${u}|${trimmed}`];
  if (stripped && stripped !== trimmed) {
    keys.push(`${u}|${stripped}`);
  }
  return keys;
}

function recordSuppressedAlertChat(userName, rawMessageText, windowMs = 5000) {
  const now = Date.now();
  for (const [k, exp] of recentSuppressedChatKeys.entries()) {
    if (now > exp) recentSuppressedChatKeys.delete(k);
  }
  buildChatMatchKeys(userName, rawMessageText).forEach(k => {
    recentSuppressedChatKeys.set(k, now + windowMs);
  });
}

function isSuppressedAlertChat(userName, rawMessageText) {
  const now = Date.now();
  const keys = buildChatMatchKeys(userName, rawMessageText);
  return keys.some(k => {
    const exp = recentSuppressedChatKeys.get(k);
    return exp && now <= exp;
  });
}

/* ========================================================
 * 1. Twitch IRC WebSocket 接続
 * ======================================================== */
let ircWs = null;
let ircReconnectTimer = null;

function parseIrcTags(tagStr) {
  const tags = {};
  if (!tagStr) return tags;
  tagStr.split(';').forEach(pair => {
    const eq = pair.indexOf('=');
    if (eq !== -1) {
      tags[pair.slice(0, eq)] = pair.slice(eq + 1);
    }
  });
  return tags;
}

function buildFragmentsFromIrc(messageText, emotesTag) {
  if (!emotesTag) {
    return [{ type: 'text', text: messageText }];
  }
  const replacements = [];
  emotesTag.split('/').forEach(group => {
    const [emoteId, rangesStr] = group.split(':');
    if (!emoteId || !rangesStr) return;
    rangesStr.split(',').forEach(r => {
      const [startStr, endStr] = r.split('-');
      const start = parseInt(startStr, 10);
      const end = parseInt(endStr, 10);
      if (!isNaN(start) && !isNaN(end)) {
        replacements.push({ start, end, emoteId });
      }
    });
  });

  if (replacements.length === 0) {
    return [{ type: 'text', text: messageText }];
  }

  replacements.sort((a, b) => a.start - b.start);
  const chars = Array.from(messageText);
  const fragments = [];
  let cursor = 0;

  replacements.forEach(rep => {
    if (rep.start > cursor) {
      fragments.push({
        type: 'text',
        text: chars.slice(cursor, rep.start).join('')
      });
    }
    const emoteCode = chars.slice(rep.start, rep.end + 1).join('');
    fragments.push({
      type: 'emote',
      emoteId: rep.emoteId,
      text: emoteCode
    });
    cursor = rep.end + 1;
  });

  if (cursor < chars.length) {
    fragments.push({
      type: 'text',
      text: chars.slice(cursor).join('')
    });
  }
  return fragments;
}

function extractRoleBadgesFromIrc(badgesTag) {
  const list = [];
  if (!badgesTag) return list;
  if (badgesTag.includes('broadcaster/')) list.push({ label: '配信者', cls: 'badge-broadcaster' });
  if (badgesTag.includes('moderator/'))   list.push({ label: 'MOD', cls: 'badge-mod' });
  if (badgesTag.includes('vip/'))         list.push({ label: 'VIP', cls: 'badge-vip' });
  if (badgesTag.includes('subscriber/') || badgesTag.includes('founder/')) {
    list.push({ label: 'SUB', cls: 'badge-sub' });
  }
  return list;
}

function handleIrcPrivmsg(tags, rest) {
  const msgMatch = /PRIVMSG #\S+ :(.*)$/.exec(rest);
  const msgText = msgMatch ? msgMatch[1].replace(/^\u0001ACTION (.*)\u0001$/, '$1') : '';
  const loginMatch = /:([^!]+)!/.exec(rest);
  const loginName = loginMatch ? loginMatch[1] : 'user';
  const displayName = tags['display-name'] || loginName;
  const userColor = tags['color'] || '#a970ff';
  const msgId = tags['id'] || '';

  // チャネポ引き換えコメント: EventSub (channel.channel_points_custom_reward_redemption.add) 側で
  // アラートカード1枚として描画するため、IRC側では messageId と抑制キーを登録してスキップ
  if (tags['custom-reward-id']) {
    if (msgId) markProcessed(msgId);
    if (msgText) {
      recordSuppressedAlertChat(displayName, msgText);
      recordSuppressedAlertChat(loginName, msgText);
      cancelPendingChatTimer(displayName, msgText);
      cancelPendingChatTimer(loginName, msgText);
    }
    return;
  }

  // コメント付きビッツ (Cheer): EventSub (channel.cheer) 側でアラートカード1枚に統合して描画するため、
  // IRC側での重複アラート発火を止め、さらに同じ messageId・本文が通常チャットとして表示されないよう登録
  if (tags['bits']) {
    if (msgId) markProcessed(msgId);
    if (msgText) {
      recordSuppressedAlertChat(displayName, msgText);
      recordSuppressedAlertChat(loginName, msgText);
      cancelPendingChatTimer(displayName, msgText);
      cancelPendingChatTimer(loginName, msgText);
    }
    return;
  }

  if (isSuppressedAlertChat(displayName, msgText) || isSuppressedAlertChat(loginName, msgText)) {
    if (msgId) markProcessed(msgId);
    return;
  }

  const fragments = buildFragmentsFromIrc(msgText, tags['emotes'] || '');
  const badges = extractRoleBadgesFromIrc(tags['badges'] || '');

  handleIncomingChat({
    platform: 'twitch',
    messageId: msgId,
    userLogin: loginName,
    userName: displayName,
    userColor,
    badges,
    fragments,
    rawText: msgText
  });
}

function handleIrcUsernotice(tags, rest) {
  const msgIdType = tags['msg-id'] || '';
  const sysMsg = (tags['system-msg'] || '').replace(/\\s/g, ' ');
  const displayName = tags['display-name'] || tags['login'] || 'リスナー';
  const loginName = (tags['login'] || displayName).toLowerCase();
  const userMsgMatch = /USERNOTICE #\S+ :(.*)$/.exec(rest);
  const userMsg = userMsgMatch ? userMsgMatch[1] : '';
  const noticeId = tags['id'] || `${msgIdType}-${Date.now()}`;

  if (noticeId) markProcessed(noticeId);

  if (msgIdType === 'sub') {
    const sig = `sub-new:${loginName}`;
    if (isDuplicateAlertSignature(sig)) return;
    handleIncomingAlert({
      alertId: `sub-${noticeId}`,
      kind: 'sub',
      typeLabel: '⭐ NEW SUBSCRIBE!',
      titleText: sysMsg || `${displayName} さんが新規サブスクライブしました！`,
      userInput: userMsg
    });
  } else if (msgIdType === 'resub') {
    const months = tags['msg-param-cumulative-months'] || '1';
    if (userMsg) {
      recordSuppressedAlertChat(displayName, userMsg);
      recordSuppressedAlertChat(loginName, userMsg);
      cancelPendingChatTimer(displayName, userMsg);
    }
    const sig = `resub:${loginName}:${months}`;
    if (isDuplicateAlertSignature(sig)) return;
    handleIncomingAlert({
      alertId: `resub-${noticeId}`,
      kind: 'sub',
      typeLabel: `⭐ RESUBSCRIBE (${months}ヶ月継続)`,
      titleText: `${displayName} さんが ${months}ヶ月目のサブスク継続です！`,
      userInput: userMsg
    });
  } else if (msgIdType === 'submysterygift') {
    const massCount = tags['msg-param-mass-gift-count'] || '1';
    const sig = `subgift:${loginName}:${massCount}`;
    if (isDuplicateAlertSignature(sig)) return;
    handleIncomingAlert({
      alertId: `subgift-${noticeId}`,
      kind: 'sub',
      typeLabel: `🎁 SUB GIFT x${massCount}!`,
      titleText: `${displayName} さんが ${massCount} 個のサブスクギフトを贈りました！`,
      ttsText: `${displayName}さんが、${massCount}個のサブスクギフトを贈ってくれました！`
    });
  } else if (msgIdType === 'subgift') {
    // コミュニティギフト(submysterygift)に付随して人数分流れる個別の subgift 通知は重複防止のためスキップ
    // 単発の指名ギフトの場合も EventSub (channel.subscription.gift) と署名キーで重複排除
    if (tags['msg-param-origin-id'] || recentAlertSignatures.has(`subgift- origin:${loginName}`)) {
      return;
    }
    const sig = `subgift:${loginName}:1`;
    if (isDuplicateAlertSignature(sig)) return;
    handleIncomingAlert({
      alertId: `subgift-${noticeId}`,
      kind: 'sub',
      typeLabel: '🎁 SUB GIFT x1!',
      titleText: sysMsg || `${displayName} さんがサブスクギフトを贈りました！`,
      userInput: userMsg
    });
  } else if (msgIdType === 'raid') {
    const viewerCount = tags['msg-param-viewerCount'] || '1';
    const raider = tags['msg-param-displayName'] || displayName;
    const raiderKey = (tags['msg-param-login'] || raider).toLowerCase();
    const sig = `raid:${raiderKey}`;
    if (isDuplicateAlertSignature(sig)) return;
    handleIncomingAlert({
      alertId: `raid-${noticeId}`,
      kind: 'raid',
      typeLabel: '🚀 INCOMING RAID!',
      titleText: `${raider} さんから ${viewerCount} 人でレイドが到着しました！`,
      ttsText: `${raider}さんから、${viewerCount}人でレイドが到着しました！いらっしゃいませ！`
    });
  } else if (msgIdType === 'viewermilestone') {
    // 連続視聴記録シェア (watch streak)。チャネポとは別系統で USERNOTICE で届く (Issue #42)
    const streak = tags['msg-param-threshold'] || '';
    const sig = `milestone:${loginName}:${streak}`;
    if (isDuplicateAlertSignature(sig)) return;
    handleIncomingAlert({
      alertId: `milestone-${noticeId}`,
      kind: 'milestone',
      typeLabel: '🔥 WATCH STREAK!',
      titleText: sysMsg || `${displayName} さんが連続視聴記録をシェアしました！`,
      userInput: userMsg
    });
  }
}

// IRC WebSocket の生テキストフレームを処理する関数（本番通信・重複テスト発火の両方で共用）
function processRawIrcData(rawData, chName = channelName) {
  const ch = (chName || '').trim().toLowerCase();
  const lines = String(rawData || '').split(/\r\n|\r|\n/);
  for (const line of lines) {
    if (!line) continue;
    if (line.startsWith('PING')) {
      if (ircWs && ircWs.readyState === WebSocket.OPEN) {
        ircWs.send('PONG :tmi.twitch.tv');
      }
      continue;
    }
    if (line.includes(`JOIN #${ch}`) || line.includes(':End of /NAMES list')) {
      dotIrc.className = 'dot ok';
      setLog(`✓ Chat接続完了 (#${ch})`);
    }

    let tagPart = '';
    let rest = line;
    if (line.startsWith('@')) {
      const spIdx = line.indexOf(' ');
      if (spIdx !== -1) {
        tagPart = line.slice(1, spIdx);
        rest = line.slice(spIdx + 1);
      }
    }
    const tags = parseIrcTags(tagPart);

    if (rest.includes('PRIVMSG')) {
      handleIrcPrivmsg(tags, rest);
    }
    if (rest.includes('USERNOTICE')) {
      handleIrcUsernotice(tags, rest);
    }
    if (rest.includes('CLEARMSG')) {
      handleIrcClearmsg(tags, rest);
    }
    if (rest.includes('CLEARCHAT')) {
      handleIrcClearchat(tags, rest);
    }
  }
}

// 削除通知 (UC-09): 履歴に削除マーカーを残すため FeedLog へ送る。
// 表示側のカードは従来どおり触らない (履歴でのみ削除を追えるようにする)。
function handleIrcClearmsg(tags, rest) {
  if (!window.FeedLog) return;
  const targetId = tags['target-msg-id'] || '';
  FeedLog.record({
    kind: 'deletion',
    id: targetId ? `del:${targetId}` : undefined,
    targetId,
    userLogin: tags['login'] || ''
  });
}

function handleIrcClearchat(tags, rest) {
  if (!window.FeedLog) return;
  const m = /CLEARCHAT #\S+(?: :(\S+))?/.exec(rest);
  const targetUser = (m && m[1]) || '';
  FeedLog.record({
    kind: 'deletion',
    id: `clear:${targetUser || 'all'}:${tags['tmi-sent-ts'] || ''}`,
    targetUser,
    banDuration: tags['ban-duration'] || ''
  });
}

function connectIrcChat(channel) {
  if (ircReconnectTimer) {
    clearTimeout(ircReconnectTimer);
    ircReconnectTimer = null;
  }
  if (ircWs) {
    try {
      ircWs.onclose = null;
      ircWs.onmessage = null;
      ircWs.close();
    } catch (e) {}
    ircWs = null;
  }
  const ch = (channel || channelName || '').trim().toLowerCase();
  if (!ch) {
    // サーバー起動直後等でチャンネル名がまだ取得できていない場合は2秒後に再試行（空の # へJOINして放置されるのを防止）
    dotIrc.className = 'dot warn';
    setLog('⏳ Twitchチャンネル設定の読み込みを待機しています...');
    ircReconnectTimer = setTimeout(() => {
      ircReconnectTimer = null;
      connectIrcChat(channelName);
    }, 2000);
    return;
  }
  dotIrc.className = 'dot warn';
  ircWs = new WebSocket('wss://irc-ws.chat.twitch.tv:443');

  ircWs.onopen = () => {
    ircWs.send('CAP REQ :twitch.tv/tags twitch.tv/commands');
    ircWs.send('PASS SCHMOOPIE');
    ircWs.send(`NICK justinfan${Math.floor(Math.random() * 80000 + 1000)}`);
    ircWs.send(`JOIN #${ch}`);
  };

  ircWs.onmessage = (event) => {
    processRawIrcData(event.data, ch);
  };

  ircWs.onclose = () => {
    dotIrc.className = 'dot';
    if (ircReconnectTimer) clearTimeout(ircReconnectTimer);
    ircReconnectTimer = setTimeout(() => {
      ircReconnectTimer = null;
      connectIrcChat(channelName);
    }, 3500);
  };
}

/* ========================================================
 * 2. Twitch EventSub WebSocket 接続
 * ======================================================== */
let eventSubWs = null;
let eventSubReconnectTimer = null;
// 購読 POST が 401 (トークン未設定・失効) の終状態フラグ (Issue #178 [4-1])。
// 立っている間は再接続・ヘルスチェックの自動復旧を行わず、認可完了の
// 通知 (BroadcastChannel auth_updated -> initViewer) からの再開を待つ。
let eventSubAuthBlocked = false;
// session_reconnect 移行中の先行接続ソケット (Issue #178 [4-3])
let eventSubMigratingWs = null;
let pendingChatTimers = new Map();

// 保留中の通常チャット予約タイマーをキャンセルするヘルパー（チャネポ・ビッツ・継続サブスク対応）
function cancelPendingChatTimer(userName, rawMessageText) {
  buildChatMatchKeys(userName, rawMessageText).forEach(key => {
    if (pendingChatTimers.has(key)) {
      clearTimeout(pendingChatTimers.get(key));
      pendingChatTimers.delete(key);
    }
  });
}

function handleEventSubNotification(subType, event) {
  if (subType === 'channel.chat.message') {
    const msgId = event.message_id;
    const msgText = (event.message && event.message.text) || '';
    const userName = event.chatter_user_name || event.chatter_user_login || 'リスナー';
    const userLogin = event.chatter_user_login || userName;

    // 1) EventSub の channel.chat.message ペイロード自体に cheer またはチャネポ報酬IDが含まれている場合は即スキップ
    const hasCheermoteFragment = Array.isArray(event.message && event.message.fragments) &&
      event.message.fragments.some(f => f && f.type === 'cheermote');
    if (event.cheer || hasCheermoteFragment || event.channel_points_custom_reward_id) {
      if (msgId) markProcessed(msgId);
      recordSuppressedAlertChat(userName, msgText);
      recordSuppressedAlertChat(userLogin, msgText);
      return;
    }

    // 2) もし channel.cheer や redemption.add が数ミリ秒先に届いていた場合も、抑制リストに一致すれば即スキップ
    if (isSuppressedAlertChat(userName, msgText) || isSuppressedAlertChat(userLogin, msgText)) {
      if (msgId) markProcessed(msgId);
      return;
    }

    const rawFragments = (event.message && event.message.fragments) || [];
    const fragments = rawFragments.map(f => {
      if (f.type === 'emote' && f.emote && f.emote.id) {
        return { type: 'emote', emoteId: f.emote.id, text: f.text };
      }
      return { type: 'text', text: f.text || '' };
    });

    const badges = [];
    (event.badges || []).forEach(b => {
      if (b.set_id === 'broadcaster') badges.push({ label: '配信者', cls: 'badge-broadcaster' });
      else if (b.set_id === 'moderator') badges.push({ label: 'MOD', cls: 'badge-mod' });
      else if (b.set_id === 'vip') badges.push({ label: 'VIP', cls: 'badge-vip' });
      else if (b.set_id === 'subscriber' || b.set_id === 'founder') badges.push({ label: 'SUB', cls: 'badge-sub' });
    });

    const chatData = {
      platform: 'twitch',
      messageId: msgId,
      userLogin: userLogin,
      userName: userName,
      userColor: event.color || '#a970ff',
      badges,
      fragments,
      rawText: msgText
    };

    // 300msの遅延をかけ、タイマーをMapに保持する（チャネポやビッツ通知が後から届いた場合のキャンセル用）
    const matchKeys = buildChatMatchKeys(userName, msgText);
    const primaryKey = matchKeys[0] || `${userName.toLowerCase()}|${msgText.trim()}`;
    if (pendingChatTimers.has(primaryKey)) {
      clearTimeout(pendingChatTimers.get(primaryKey));
    }

    const timer = setTimeout(() => {
      matchKeys.forEach(k => pendingChatTimers.delete(k));
      if (isSuppressedAlertChat(userName, msgText) || isSuppressedAlertChat(userLogin, msgText)) {
        return;
      }
      handleIncomingChat(chatData); // 300ms以内に消されなければ通常の黒チャットとして描画
    }, 300);

    matchKeys.forEach(k => pendingChatTimers.set(k, timer));
  }
  else if (subType === 'channel.channel_points_custom_reward_redemption.add') {
    const userName = event.user_name || event.user_login || 'リスナー';
    const userLogin = event.user_login || userName;
    const rewardTitle = (event.reward && event.reward.title) || 'チャンネルポイント報酬';
    const rewardCost = (event.reward && event.reward.cost) ? ` (${event.reward.cost}pt)` : '';
    const userInput = event.user_input || '';

    // 先または後に届く通常チャット(黒カード)を両方ブロック
    if (userInput) {
      recordSuppressedAlertChat(userName, userInput);
      recordSuppressedAlertChat(userLogin, userInput);
      cancelPendingChatTimer(userName, userInput);
      cancelPendingChatTimer(userLogin, userInput);
    }

    const sig = `redemption:${userLogin.toLowerCase()}:${rewardTitle}:${userInput.trim()}`;
    if (isDuplicateAlertSignature(sig, 5000)) return;

    handleIncomingAlert({
      alertId: `redemption-${event.id || Date.now()}`,
      kind: 'redemption',
      typeLabel: `🎁 チャンネルポイント${rewardCost}`,
      titleText: `${userName} さんが「${rewardTitle}」を引き換えました！`,
      userInput: userInput,
      userName: userName,
      userLogin: userLogin,
      userColor: '#10b981',
      ttsText: userInput
        ? `${userName}さんが、チャンネルポイントで、${rewardTitle}を引き換えました。${userInput}`
        : `${userName}さんが、チャンネルポイントで、${rewardTitle}を引き換えました。`
    });
  }
  else if (subType === 'channel.follow') {
    const userName = event.user_name || event.user_login || 'リスナー';
    const userKey = (event.user_id || event.user_login || userName).toLowerCase();
    if (isDuplicateAlertSignature(`follow:${userKey}`)) return;

    handleIncomingAlert({
      alertId: `follow-${event.user_id || userName}`,
      kind: 'follow',
      typeLabel: '❤️ NEW FOLLOWER!',
      titleText: `${userName} さんがフォローしてくれました！ありがとうございます！`,
      userName: userName,
      userLogin: userKey,
      userColor: '#ec4899',
      ttsText: `${userName}さんがフォローしてくれました。ありがとうございます！`
    });
  }
  else if (subType === 'channel.subscribe') {
    if (event.is_gift) return;
    const userName = event.user_name || event.user_login || 'リスナー';
    const loginKey = (event.user_login || userName).toLowerCase();
    if (isDuplicateAlertSignature(`sub-new:${loginKey}`)) return;

    const tierMap = { '1000': 'Tier 1', '2000': 'Tier 2', '3000': 'Tier 3' };
    const tier = tierMap[event.tier] || 'サブスク';
    handleIncomingAlert({
      alertId: `eventsub-sub-${event.user_id || loginKey}-${Date.now()}`,
      kind: 'sub',
      typeLabel: `⭐ NEW SUBSCRIBE (${tier})`,
      titleText: `${userName} さんが新規サブスクライブしました！`,
      userName: userName,
      userLogin: loginKey,
      userColor: '#a855f7',
      ttsText: `${userName}さんが新規サブスクライブしてくれました！ありがとうございます！`
    });
  }
  else if (subType === 'channel.subscription.message') {
    const userName = event.user_name || event.user_login || 'リスナー';
    const loginKey = (event.user_login || userName).toLowerCase();
    const months = String(event.cumulative_months || 1);
    const userMsg = (event.message && event.message.text) || '';

    if (userMsg) {
      recordSuppressedAlertChat(userName, userMsg);
      recordSuppressedAlertChat(loginKey, userMsg);
      cancelPendingChatTimer(userName, userMsg);
      cancelPendingChatTimer(loginKey, userMsg);
    }

    if (isDuplicateAlertSignature(`resub:${loginKey}:${months}`)) return;

    handleIncomingAlert({
      alertId: `eventsub-resub-${event.user_id || loginKey}-${months}`,
      kind: 'sub',
      typeLabel: `⭐ RESUBSCRIBE (${months}ヶ月継続)`,
      titleText: `${userName} さんが ${months}ヶ月目のサブスク継続です！`,
      userInput: userMsg,
      userName: userName,
      userLogin: loginKey,
      userColor: '#a855f7'
    });
  }
  else if (subType === 'channel.subscription.gift') {
    const gifter = event.is_anonymous ? '匿名サンタ' : (event.user_name || event.user_login || 'リスナー');
    const loginKey = event.is_anonymous ? 'anonymous' : (event.user_login || gifter).toLowerCase();
    const total = String(event.total || 1);
    recentAlertSignatures.set(`subgift-origin:${loginKey}`, Date.now());
    if (isDuplicateAlertSignature(`subgift:${loginKey}:${total}`)) return;

    handleIncomingAlert({
      alertId: `eventsub-gift-${event.id || Date.now()}`,
      kind: 'sub',
      typeLabel: `🎁 SUB GIFT x${total}!`,
      titleText: `${gifter} さんが ${total} 個のサブスクギフトを贈りました！`,
      userName: gifter,
      userLogin: loginKey,
      userColor: '#a855f7',
      ttsText: `${gifter}さんが、${total}個のサブスクギフトを贈ってくれました！`
    });
  }
  else if (subType === 'channel.cheer') {
    const cheerer = event.is_anonymous ? '匿名チアー' : (event.user_name || event.user_login || 'リスナー');
    const loginKey = event.is_anonymous ? 'anonymous' : (event.user_login || cheerer).toLowerCase();
    const bits = event.bits || 100;
    const rawMsg = event.message || '';
    const cleanedMsg = stripCheermoteTokens(rawMsg);

    // 先または後に届く通常チャット(黒カード)を両方ブロック
    if (rawMsg) {
      recordSuppressedAlertChat(cheerer, rawMsg);
      recordSuppressedAlertChat(loginKey, rawMsg);
      cancelPendingChatTimer(cheerer, rawMsg);
      cancelPendingChatTimer(loginKey, rawMsg);
    }

    const sig = `cheer:${loginKey}:${bits}:${cleanedMsg}`;
    if (isDuplicateAlertSignature(sig, 5000)) return;

    handleIncomingAlert({
      alertId: `eventsub-cheer-${event.id || Date.now()}`,
      kind: 'cheer',
      typeLabel: `💎 ${bits} BITS CHEER!`,
      titleText: `${cheerer} さんが ${bits} ビッツで応援してくれました！`,
      userInput: cleanedMsg,
      userName: cheerer,
      userLogin: loginKey,
      userColor: '#f59e0b',
      ttsText: cleanedMsg
        ? `${cheerer}さんが、${bits}ビッツで応援してくれました！${cleanedMsg}`
        : `${cheerer}さんが、${bits}ビッツで応援してくれました！`
    });
  }
  else if (subType === 'channel.raid') {
    const raider = event.from_broadcaster_user_name || event.from_broadcaster_user_login || '配信者';
    const raiderKey = (event.from_broadcaster_user_login || raider).toLowerCase();
    const viewers = event.viewers || 1;
    if (isDuplicateAlertSignature(`raid:${raiderKey}`)) return;

    handleIncomingAlert({
      alertId: `eventsub-raid-${raiderKey}-${Date.now()}`,
      kind: 'raid',
      typeLabel: '🚀 INCOMING RAID!',
      titleText: `${raider} さんから ${viewers} 人でレイドが到着しました！`,
      userName: raider,
      userLogin: raiderKey,
      userColor: '#ef4444',
      ttsText: `${raider}さんから、${viewers}人でレイドが到着しました！いらっしゃいませ！`
    });
  }
  else if (subType === 'stream.online') {
    // 配信マーカー (UC-09): タイトル・カテゴリを付けて記録を開始する
    if (window.FeedLog) {
      const startedAt = event.started_at || '';
      (async () => {
        let title = '';
        let category = '';
        try {
          const res = await authFetch(`${API_BASE}/api/twitch/status`);
          const data = await res.json();
          if (data && data.channel_info) {
            title = data.channel_info.title || '';
            category = data.channel_info.game_name || '';
          }
        } catch (e) {}
        FeedLog.streamStarted({ title, category, startedAt });
      })();
    }
  }
  else if (subType === 'stream.offline') {
    if (window.FeedLog) FeedLog.streamEnded();
  }
  else if (subType && subType.startsWith('channel.hype_train.')) {
    // ハイプトレイン (UC-09): カード表示はせず、履歴にだけ残す
    if (window.FeedLog) {
      const phase = subType.slice('channel.hype_train.'.length);
      const level = event.level || 0;
      const total = event.total || 0;
      FeedLog.record({
        kind: 'alert',
        id: `hypetrain:${event.id || 'train'}:${phase}:${total}`,
        source: 'twitch',
        alertKind: `hype_train_${phase}`,
        level,
        total,
        goal: event.goal || 0,
        progress: event.progress || 0
      });
    }
  }
}

function scheduleEventSubReconnect(delayMs = 4000) {
  // 認可待ちの終状態では再接続を予約しない (Issue #178 [4-1])
  if (eventSubAuthBlocked) return;
  if (eventSubReconnectTimer) clearTimeout(eventSubReconnectTimer);
  eventSubReconnectTimer = setTimeout(() => {
    eventSubReconnectTimer = null;
    // 予約後に 401 終状態へ遷移していたら再接続しない（タイマー予約と
    // 終状態化の競合対策。PR #193 round-1 レビュー指摘 #2）
    if (eventSubAuthBlocked) return;
    connectEventSubWebSocket();
  }, delayMs);
}

function connectEventSubWebSocket() {
  if (eventSubReconnectTimer) {
    clearTimeout(eventSubReconnectTimer);
    eventSubReconnectTimer = null;
  }
  // 明示的な接続開始は新しい試行として扱い、終状態を解除する。
  // 認可完了 (auth_updated -> initViewer) からの購読再開もこの経路。
  eventSubAuthBlocked = false;
  if (eventSubMigratingWs) {
    try {
      eventSubMigratingWs.onclose = null;
      eventSubMigratingWs.onmessage = null;
      eventSubMigratingWs.close();
    } catch (e) {}
    eventSubMigratingWs = null;
  }
  if (eventSubWs) {
    try {
      eventSubWs.onclose = null;
      eventSubWs.onmessage = null;
      eventSubWs.close();
    } catch (e) {}
    eventSubWs = null;
  }
  dotEventSub.className = 'dot warn';
  eventSubWs = new WebSocket('wss://eventsub.wss.twitch.tv/ws');
  attachEventSubSocketHandlers(eventSubWs, null);
}

// session_reconnect による無瞬断移行 (Issue #178 [4-3]):
// reconnect_url へ先行接続し、新ソケットの session_welcome を受けた
// 時点で旧ソケットを閉じる。購読は Twitch 側で新セッションへ移行
// されるため、移行時は購読 POST を再送しない。
function migrateEventSubWebSocket(reconnectUrl) {
  if (!reconnectUrl || eventSubMigratingWs) return;
  const ws = new WebSocket(reconnectUrl);
  eventSubMigratingWs = ws;
  attachEventSubSocketHandlers(ws, eventSubWs);
}

function attachEventSubSocketHandlers(ws, migrationFrom) {
  ws.onmessage = async (ev) => {
    let packet = null;
    try { packet = JSON.parse(ev.data); } catch (e) { return; }
    const meta = packet.metadata || {};
    const payload = packet.payload || {};

    if (meta.message_type === 'session_welcome') {
      const sessionId = payload.session && payload.session.id;
      if (!sessionId) return;
      if (migrationFrom) {
        // 移行完了: このソケットを本線にし、旧ソケットを閉じる。
        // 購読は Twitch 側で移行済みのため、購読 POST は再送しない。
        if (eventSubMigratingWs === ws) eventSubMigratingWs = null;
        eventSubWs = ws;
        // 移行完了時点で予約済みの再接続タイマーが残っていると、移行済みの
        // 正常な接続を最大 4 秒後に強制再接続してしまうため解除する
        // (PR #193 round-1 レビュー指摘 #3)
        if (eventSubReconnectTimer) {
          clearTimeout(eventSubReconnectTimer);
          eventSubReconnectTimer = null;
        }
        try {
          migrationFrom.onclose = null;
          migrationFrom.onmessage = null;
          migrationFrom.close();
        } catch (e) {}
        dotEventSub.className = 'dot ok';
        setLog('🔄 EventSub セッションを移行しました（購読は自動で引き継がれます）');
        return;
      }
      try {
        const res = await authFetch(`${API_BASE}/api/twitch/eventsub/subscribe`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId })
        });
        // 購読 POST の待機中にソケットが切り替わっていたら、この応答は
        // 旧ソケットのものとして破棄する（遅延 401 が新接続の状態を
        // 上書きしないようにする。PR #193 round-1 レビュー指摘 #2）。
        // この分岐に到達するのは移行なし（migrationFrom === null）の場合のみ。
        if (eventSubWs !== ws) return;
        if (res.status === 401) {
          // トークン未設定・失効はユーザーが認可するまで回復しない終状態
          // (Issue #178 [4-1])。再接続を予約せずソケットを閉じ、認可完了の
          // 通知 (auth_updated -> initViewer -> connectEventSubWebSocket) で再開する。
          // 予約済みの再接続タイマーも解除し、発火時の再接続を防ぐ。
          if (eventSubReconnectTimer) {
            clearTimeout(eventSubReconnectTimer);
            eventSubReconnectTimer = null;
          }
          eventSubAuthBlocked = true;
          dotEventSub.className = 'dot';
          dotEventSub.title = 'EventSub 未認可（トークン未設定・失効）';
          scopeWarningBox.style.display = 'block';
          setLog('🔑 EventSub の認可が必要です。Twitch 認可を完了すると自動で再接続します');
          if (eventSubWs === ws) {
            try { ws.onclose = null; ws.close(); } catch (e) {}
            eventSubWs = null;
          }
          return;
        }
        if (res.status >= 500) {
          dotEventSub.className = 'dot warn';
          dotEventSub.title = `EventSub 一時エラー (HTTP ${res.status})`;
          setLog(`⚠️ Twitch 検証サーバーとの通信に失敗しました (HTTP ${res.status})。再接続します...`);
          scheduleEventSubReconnect(4000);
          return;
        }
        const subData = await res.json();
        if (res.ok && Array.isArray(subData.results)) {
          const okCount = subData.results.filter(r => r.success).length;
          const total = subData.results.length;
          if (okCount === total) {
            dotEventSub.className = 'dot ok';
            dotEventSub.title = `EventSub全${total}種 購読完了`;
            scopeWarningBox.style.display = 'none';
            setLog(`✓ EventSub 全${total}種アラート＆チャット購読完了！`);
          } else if (okCount > 0) {
            // 一部失敗は終状態（再接続しても直らない）。失敗の内訳に応じて表示を分ける:
            // 403 が含まれるときだけスコープ不足として再認可案内を出し、それ以外は失敗タイプをログに残す
            const failed = subData.results.filter(r => !r.success);
            const hasScopeFailure = failed.some(r => r.code === 403);
            dotEventSub.className = 'dot warn';
            if (hasScopeFailure) {
              dotEventSub.title = `EventSub ${okCount}/${total}種 購読中（一部スコープ不足）`;
              scopeWarningBox.style.display = 'block';
            } else {
              dotEventSub.title = `EventSub ${okCount}/${total}種 購読中（一部購読失敗）`;
              scopeWarningBox.style.display = 'none';
            }
            setLog(`⚠️ EventSub 購読失敗: ${failed.map(r => `${r.type} (HTTP ${r.code})`).join(', ')}`);
          } else {
            dotEventSub.className = 'dot';
            scopeWarningBox.style.display = 'block';
          }
        } else {
          dotEventSub.className = 'dot';
          scopeWarningBox.style.display = 'block';
          scheduleEventSubReconnect(4000);
        }
      } catch (e) {
        // OBS起動直後でローカルサーバー(server.py)がまだ立ち上がりきっていない場合、
        // Twitch EventSub の10秒制限が切れる前にソケットを作り直して自動リトライする
        dotEventSub.className = 'dot warn';
        setLog('⏳ ローカルサーバー起動待機中... EventSubを再接続します');
        scheduleEventSubReconnect(2500);
      }
    }

    if (meta.message_type === 'session_reconnect') {
      // 現行ソケットに対する移行指示のときだけ従う
      if (eventSubWs === ws) {
        const reconnectUrl = payload.session && payload.session.reconnect_url;
        migrateEventSubWebSocket(reconnectUrl);
      }
    }

    if (meta.message_type === 'notification') {
      // 移行過渡期に新旧両ソケットへ同一 notification が重複配信される
      // ことがあるため、metadata.message_id で重複排除する
      // (PR #193 round-1 レビュー指摘 #4)。
      if (meta.message_id && markProcessed(`eventsub:${meta.message_id}`)) return;
      const subType = payload.subscription && payload.subscription.type;
      const event = payload.event || {};
      handleEventSubNotification(subType, event);
    }
  };

  ws.onclose = () => {
    if (eventSubMigratingWs === ws) {
      // 移行が welcome 前に失敗した場合は旧ソケット側のライフサイクルに委ねる
      eventSubMigratingWs = null;
      return;
    }
    if (eventSubWs !== ws) return;
    dotEventSub.className = 'dot';
    scheduleEventSubReconnect(4000);
  };
}

/* ========================================================
 * 3. 本番と同一の「重複発火パケット」を受信入口へ流し込む統合シミュレーター
 * ======================================================== */
function simulateTwitchEventPacketBurst(kind) {
  const now = Date.now();
  const msgId = `sim-msg-${now}`;
  const ch = (channelName || '').toLowerCase();

  if (kind === 'jp-chat') {
    const text = 'こんばんは！今日のスプラ3配信も楽しみにしてました！';
    // ① IRC PRIVMSG 生パケット + ② EventSub channel.chat.message 生パケット の同時受信をシミュレート
    processRawIrcData(`@badge-info=;badges=subscriber/6;color=#38bdf8;display-name=イカちゃんリスナー;emotes=;id=${msgId} :ikachan!ikachan@ikachan.tmi.twitch.tv PRIVMSG #${ch} :${text}`);
    handleEventSubNotification('channel.chat.message', {
      message_id: msgId,
      chatter_user_name: 'イカちゃんリスナー',
      chatter_user_login: 'ikachan',
      color: '#38bdf8',
      badges: [{ set_id: 'subscriber' }],
      message: { text, fragments: [{ type: 'text', text }] }
    });
  }
  else if (kind === 'en-chat') {
    const text = 'Hello from California! Your keyboard chord playing sounds amazing! Kappa';
    // ① IRC PRIVMSG + ② EventSub channel.chat.message の同時受信
    processRawIrcData(`@badges=;color=#f59e0b;display-name=Alex_From_USA;emotes=25:68-72;id=${msgId} :alex!alex@alex.tmi.twitch.tv PRIVMSG #${ch} :${text}`);
    handleEventSubNotification('channel.chat.message', {
      message_id: msgId,
      chatter_user_name: 'Alex_From_USA',
      chatter_user_login: 'alex',
      color: '#f59e0b',
      badges: [],
      message: {
        text,
        fragments: [
          { type: 'text', text: 'Hello from California! Your keyboard chord playing sounds amazing! ' },
          { type: 'emote', text: 'Kappa', emote: { id: '25' } }
        ]
      }
    });
  }
  else if (kind === 'fr-chat' || kind === 'emote-chat') {
    const text = 'Tu joues à ce jeu depuis combien de temps ? PogChamp';
    processRawIrcData(`@badges=;color=#38bdf8;display-name=Pierre_Paris;emotes=305954156:44-51;id=${msgId} :pierre!pierre@pierre.tmi.twitch.tv PRIVMSG #${ch} :${text}`);
    handleEventSubNotification('channel.chat.message', {
      message_id: msgId,
      chatter_user_name: 'Pierre_Paris',
      chatter_user_login: 'pierre',
      color: '#38bdf8',
      badges: [],
      message: {
        text,
        fragments: [
          { type: 'text', text: 'Tu joues à ce jeu depuis combien de temps ? ' },
          { type: 'emote', text: 'PogChamp', emote: { id: '305954156' } }
        ]
      }
    });
  }
  else if (kind === 'redemption') {
    const rawInputText = '喉を潤して次のマッチも頑張ってください！';
    // ① IRC PRIVMSG (custom-reward-id付き) + ② EventSub channel.chat.message + ③ EventSub redemption.add の3重発火
    processRawIrcData(`@color=#10b981;custom-reward-id=reward-hydration-500;display-name=タコゾネス;id=${msgId} :takozones!takozones@takozones.tmi.twitch.tv PRIVMSG #${ch} :${rawInputText}`);
    handleEventSubNotification('channel.chat.message', {
      message_id: msgId,
      chatter_user_name: 'タコゾネス',
      chatter_user_login: 'takozones',
      color: '#10b981',
      channel_points_custom_reward_id: 'reward-hydration-500',
      message: { text: rawInputText, fragments: [{ type: 'text', text: rawInputText }] },
      badges: []
    });
    handleEventSubNotification('channel.channel_points_custom_reward_redemption.add', {
      id: `sim-redemp-${now}`,
      user_name: 'タコゾネス',
      user_login: 'takozones',
      user_input: rawInputText,
      reward: { title: '水分補給タイム💧', cost: 500 }
    });
  }
  else if (kind === 'cheer') {
    const rawCheerText = 'Cheer500 コーヒー代の差し入れです☕いつも配信応援してます！';
    // ① IRC PRIVMSG (bits=500) + ② EventSub channel.chat.message + ③ EventSub channel.cheer の3重発火
    processRawIrcData(`@bits=500;color=#f59e0b;display-name=ビッツ太郎;id=${msgId} :bitstaro!bitstaro@bitstaro.tmi.twitch.tv PRIVMSG #${ch} :${rawCheerText}`);
    handleEventSubNotification('channel.chat.message', {
      message_id: msgId,
      chatter_user_name: 'ビッツ太郎',
      chatter_user_login: 'bitstaro',
      color: '#f59e0b',
      cheer: { bits: 500 },
      message: {
        text: rawCheerText,
        fragments: [
          { type: 'cheermote', text: 'Cheer500' },
          { type: 'text', text: ' コーヒー代の差し入れです☕いつも配信応援してます！' }
        ]
      },
      badges: []
    });
    handleEventSubNotification('channel.cheer', {
      id: `sim-cheer-${now}`,
      is_anonymous: false,
      user_name: 'ビッツ太郎',
      user_login: 'bitstaro',
      bits: 500,
      message: rawCheerText
    });
  }
  else if (kind === 'follow') {
    // EventSub channel.follow の重複再送パケットをシミュレート
    const ev = {
      user_id: `sim-follow-uid-${now}`,
      user_name: 'メロディ職人',
      user_login: 'melody_maker'
    };
    handleEventSubNotification('channel.follow', ev);
    handleEventSubNotification('channel.follow', ev);
  }
  else if (kind === 'sub-new') {
    // ① IRC USERNOTICE (msg-id=sub) + ② EventSub channel.subscribe (別ID) の同時到着をシミュレート
    processRawIrcData(`@display-name=すりみ連合ファン;id=irc-sub-${now};login=surimi_fan;msg-id=sub;system-msg=すりみ連合ファンさんがTier\\s1でサブスクライブしました！ :tmi.twitch.tv USERNOTICE #${ch}`);
    handleEventSubNotification('channel.subscribe', {
      user_id: `uid-surimi-${now}`,
      user_name: 'すりみ連合ファン',
      user_login: 'surimi_fan',
      tier: '1000',
      is_gift: false
    });
  }
  else if (kind === 'sub') {
    const resubText = '半年継続！これからも応援してます🎉';
    // ① IRC USERNOTICE (msg-id=resub) + ② EventSub channel.subscription.message + ③ EventSub channel.chat.message の3経路同時発火
    processRawIrcData(`@display-name=ジャッジくん;id=irc-resub-${now};login=judd_splat;msg-id=resub;msg-param-cumulative-months=6;system-msg=ジャッジくんさんが6ヶ月目のサブスク継続です！ :tmi.twitch.tv USERNOTICE #${ch} :${resubText}`);
    handleEventSubNotification('channel.subscription.message', {
      user_id: `uid-judd-${now}`,
      user_name: 'ジャッジくん',
      user_login: 'judd_splat',
      cumulative_months: 6,
      message: { text: resubText }
    });
    handleEventSubNotification('channel.chat.message', {
      message_id: `chat-resub-${now}`,
      chatter_user_name: 'ジャッジくん',
      chatter_user_login: 'judd_splat',
      color: '#a970ff',
      badges: [{ set_id: 'subscriber' }],
      message: { text: resubText, fragments: [{ type: 'text', text: resubText }] }
    });
  }
  else if (kind === 'subgift') {
    // 5個コミュニティサブギフの本番挙動:
    // ① IRC USERNOTICE (submysterygift 親通知) + ② IRC USERNOTICE (受取人ごとの subgift ×5通) + ③ EventSub channel.subscription.gift + ④ EventSub channel.subscribe (is_gift:true ×5通) の計12パケット同時発火！
    const originId = `gift-origin-${now}`;
    processRawIrcData(`@display-name=コジャッジくん;id=irc-massgift-${now};login=lil_judd;msg-id=submysterygift;msg-param-mass-gift-count=5;msg-param-origin-id=${originId} :tmi.twitch.tv USERNOTICE #${ch}`);
    for (let i = 1; i <= 5; i++) {
      processRawIrcData(`@display-name=コジャッジくん;id=irc-singlegift-${now}-${i};login=lil_judd;msg-id=subgift;msg-param-origin-id=${originId};msg-param-recipient-display-name=リスナー${i}号 :tmi.twitch.tv USERNOTICE #${ch}`);
      handleEventSubNotification('channel.subscribe', {
        user_id: `uid-recip-${i}`,
        user_name: `リスナー${i}号`,
        user_login: `listener_${i}`,
        tier: '1000',
        is_gift: true
      });
    }
    handleEventSubNotification('channel.subscription.gift', {
      id: `es-gift-${now}`,
      is_anonymous: false,
      user_name: 'コジャッジくん',
      user_login: 'lil_judd',
      total: 5
    });
  }
  else if (kind === 'raid') {
    // ① IRC USERNOTICE (msg-id=raid) + ② EventSub channel.raid の同時発火
    processRawIrcData(`@display-name=DJ_Octavio;id=irc-raid-${now};login=dj_octavio;msg-id=raid;msg-param-displayName=DJ_Octavio;msg-param-login=dj_octavio;msg-param-viewerCount=28 :tmi.twitch.tv USERNOTICE #${ch}`);
    handleEventSubNotification('channel.raid', {
      from_broadcaster_user_name: 'DJ_Octavio',
      from_broadcaster_user_login: 'dj_octavio',
      viewers: 28
    });
  }
  else if (kind === 'milestone') {
    // IRC USERNOTICE (msg-id=viewermilestone) のみ。EventSub には対応トピックがない (Issue #42)
    processRawIrcData(`@display-name=常連のタコさん;id=irc-milestone-${now};login=regular_tako;msg-id=viewermilestone;msg-param-category=watch-streak;msg-param-threshold=5;system-msg=常連のタコさん\\sis\\son\\sa\\s5\\sstream\\swatch\\sstreak! :tmi.twitch.tv USERNOTICE #${ch} :いつも楽しい配信ありがとうございます！`);
  }
}

/* ========================================================
 * ワンクリック OAuth スコープ拡張ボタン
 * ======================================================== */
btnOauthUpgrade.addEventListener('click', () => {
  window.open(`${API_BASE}/api/twitch/auth`, '_blank', 'width=560,height=720');
});


/* ========================================================
 * 初期化フロー（OBS起動直後のサーバー未起動・遅延に対する自動リトライ＆自己修復付き）
 * ======================================================== */
let isViewerInitializing = false;

async function fetchConfigWithRetry(maxAttempts = 8, intervalMs = 1200) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      // まず外部通信を含まない高速な /api/config でチャンネル名と設定を即座に取得
      const cfgRes = await fetch(`${API_BASE}/api/config`, { cache: 'no-store' });
      if (cfgRes.ok) {
        const cfgData = await cfgRes.json();
        if (cfgData.channel_name) {
          channelName = cfgData.channel_name;
          try { localStorage.setItem('obs_comment_viewer_channel_v1', channelName); } catch (_) {}
        }
        if (cfgData.viewer_settings) {
          if (!lockedUrlTheme && localStorage.getItem('theme_explicitly_selected') !== '1') {
            cfgData.viewer_settings.theme = 'default';
          }
          applySettingsToUI(cfgData.viewer_settings, false);
        }
        return true;
      }
    } catch (_) {}

    if (attempt < maxAttempts) {
      setLog(`⏳ ローカルサーバー起動待機中... (${attempt}/${maxAttempts})`);
      await new Promise(r => setTimeout(r, intervalMs));
    }
  }
  return false;
}

async function initViewer() {
  if (isViewerInitializing) return;
  isViewerInitializing = true;

  try {
    // 1. ローカル保存設定＆前回チャンネル名の即時復元
    try {
      const savedCh = localStorage.getItem('obs_comment_viewer_channel_v1');
      if (!channelName && savedCh) {
        channelName = savedCh;
      }
      const localSaved = localStorage.getItem('obs_comment_viewer_settings_v1');
      if (localSaved) {
        const parsed = JSON.parse(localSaved);
        if (!lockedUrlTheme && localStorage.getItem('theme_explicitly_selected') !== '1') {
          parsed.theme = 'default';
        }
        applySettingsToUI(parsed, false);
      }
    } catch (e) {}

    // 2. 既にチャンネル名が分かっていれば、サーバー応答を待たずにIRCチャット接続を先行スタート
    if (channelName) {
      connectIrcChat(channelName);
    }

    // 3. サーバー (/api/config) から設定を取得（OBS起動直後でserver.pyが起動中の場合は最大約10秒間自動リトライ）
    const prevChannel = channelName;
    const serverReady = await fetchConfigWithRetry(8, 1200);

    if (channelName && channelName !== prevChannel) {
      connectIrcChat(channelName);
    } else if (!ircWs || ircWs.readyState !== WebSocket.OPEN) {
      connectIrcChat(channelName);
    }

    if (channelName && typeof getTwitchUserAvatarUrl === 'function') {
      getTwitchUserAvatarUrl(channelName).catch(() => {});
    }

    // 4. EventSub WebSocket 接続＆VOICEVOX話者リスト取得を開始
    connectEventSubWebSocket();
    loadVoicevoxSpeakers();

    // 5. トークンスコープ検証は非同期でバックグラウンド確認（初期接続をブロックしない）
    if (serverReady) {
      fetch(`${API_BASE}/api/twitch/status`, { cache: 'no-store' })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!data) return;
          if (data.channel_name && data.channel_name !== channelName) {
            channelName = data.channel_name;
            try { localStorage.setItem('obs_comment_viewer_channel_v1', channelName); } catch (_) {}
            connectIrcChat(channelName);
          }
          if (
            data.token_info &&
            !('error' in data.token_info) &&
            Array.isArray(data.token_info.missing_scopes) &&
            data.token_info.missing_scopes.length > 0
          ) {
            scopeWarningBox.style.display = 'block';
          } else if (data.configured && !(data.token_info && 'error' in data.token_info)) {
            scopeWarningBox.style.display = 'none';
          }
        })
        .catch(() => {});
    } else {
      setLog('⚠️ サーバー未応答のためキャッシュ設定で動作中（自動再接続待機）');
    }
  } finally {
    isViewerInitializing = false;
  }
}

// 6. 定期ヘルスチェック（万が一IRCまたはEventSubが未接続のままだった場合に自動復旧）
setInterval(() => {
  if (!channelName || !ircWs || !dotIrc.classList.contains('ok')) {
    fetch(`${API_BASE}/api/config`, { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d && d.channel_name) {
          const changed = d.channel_name !== channelName;
          channelName = d.channel_name;
          if (changed || !ircWs || ircWs.readyState === WebSocket.CLOSED) {
            connectIrcChat(channelName);
          }
        }
      })
      .catch(() => {});
  }
  // EventSub は未接続／CLOSED のときだけ再接続する。ドットの状態（warn 等）では再接続しない:
  // 一部購読失敗は終状態であり、ドット判定で再接続すると正常な接続を定期的に切ってしまう（Issue #152）
  // 認可待ちの終状態 (Issue #178 [4-1]) も自動復旧の対象にしない
  if (!eventSubAuthBlocked && (!eventSubWs || eventSubWs.readyState === WebSocket.CLOSED)) {
    if (!eventSubReconnectTimer) {
      connectEventSubWebSocket();
    }
  }
}, 8000);
