// ========================================================
// YouTube Live チャット・スパチャ取得ポーリング (js/youtube-poller.js)
// ========================================================
'use strict';

/* ========================================================
 * YouTube Live チャット・スパチャ取得ポーリング (server.py 連携)
 * ======================================================== */
const dotYouTube = document.getElementById('dot-youtube');
const lblYtStatus = document.getElementById('lbl-yt-status');
const inpYtHandle = document.getElementById('inp-yt-handle');
const btnYtReconnect = document.getElementById('btn-yt-reconnect');
const btnYtSave = document.getElementById('btn-yt-save');

let ytLatestSeq = -1;
let ytInitialSynced = false;
let ytPollTimer = null;
let isPollingYt = false;

async function pollYouTubeLiveChat() {
  if (isPollingYt) return;
  isPollingYt = true;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 6000);

  try {
    const sinceParam = ytInitialSynced ? ytLatestSeq : -1;
    const res = await fetch(`${API_BASE}/api/youtube/poll?since=${sinceParam}`, {
      cache: 'no-store',
      signal: controller.signal
    });
    if (res.ok) {
      const data = await res.json();
      const isFirstSync = !ytInitialSynced;
      if (typeof data.latest_seq === 'number') {
        // サーバー再起動や再検出で latest_seq が巻き戻った場合も自動追従
        if (isFirstSync || data.latest_seq > ytLatestSeq || data.latest_seq < ytLatestSeq) {
          ytLatestSeq = data.latest_seq;
        }
        ytInitialSynced = true;
      }
      if (inpYtHandle && document.activeElement !== inpYtHandle) {
        const currentTarget = data.youtube_video_id || data.youtube_handle || '@your_channel';
        if (currentTarget && !inpYtHandle.dataset.edited) {
          inpYtHandle.value = currentTarget;
        }
      }

      if (data.status === 'live') {
        if (dotYouTube) {
          dotYouTube.className = 'dot ok';
          dotYouTube.title = `YouTube Live 接続中 (${data.video_id || ''})`;
        }
        if (lblYtStatus) {
          lblYtStatus.textContent = `接続中 (${data.video_id || 'LIVE'})`;
          lblYtStatus.style.color = '#4ade80';
        }
      } else if (data.status === 'offline' || data.status === 'connecting' || data.status === 'idle') {
        if (dotYouTube) {
          dotYouTube.className = 'dot warn';
          dotYouTube.title = 'YouTube Live 配信待機中（配信開始で自動接続）';
        }
        if (lblYtStatus) {
          lblYtStatus.textContent = `配信待機中 (${data.youtube_handle || '@your_channel'})`;
          lblYtStatus.style.color = '#fcd34d';
        }
      } else {
        if (dotYouTube) dotYouTube.className = 'dot';
        if (lblYtStatus) {
          lblYtStatus.textContent = data.error || '未接続';
          lblYtStatus.style.color = '#f87171';
        }
      }

      // リロード直後の初回同期時は過去ログを再描画・再読み上げせず、以降の新着イベントのみ処理する
      if (!isFirstSync && Array.isArray(data.events)) {
        for (const ev of data.events) {
          if (ev.event_type === 'chat') {
            handleIncomingChat({
              platform: 'youtube',
              messageId: ev.messageId,
              userName: ev.userName,
              userColor: ev.userColor,
              avatarUrl: ev.avatarUrl || ev.authorPhotoUrl || '',
              badges: ev.badges || [],
              fragments: ev.fragments || [],
              rawText: ev.rawText || ''
            });
          } else if (ev.event_type === 'alert') {
            handleIncomingAlert({
              platform: 'youtube',
              alertId: ev.alertId,
              kind: ev.kind || 'cheer',
              typeLabel: ev.typeLabel || 'YOUTUBE ALERT',
              titleText: ev.titleText || '',
              userInput: ev.messageText || ev.userInput || '',
              messageText: ev.messageText || '',
              ttsText: ev.ttsText || '',
              avatarUrl: ev.avatarUrl || ev.authorPhotoUrl || ''
            });
          }
        }
      }
    }
  } catch (e) {
  } finally {
    clearTimeout(timeoutId);
    isPollingYt = false;
  }
}

function startYouTubePolling() {
  if (ytPollTimer) clearInterval(ytPollTimer);
  ytInitialSynced = false;
  ytLatestSeq = -1;
  pollYouTubeLiveChat();
  ytPollTimer = setInterval(pollYouTubeLiveChat, 2000);
}

async function saveAndReconnectYouTube() {
  const val = (inpYtHandle ? inpYtHandle.value : '').trim() || '@your_channel';
  const isVideoUrlOrId = /(?:v=|youtu\.be\/|\/live\/|^[A-Za-z0-9_-]{11}$)/.test(val) && !val.startsWith('@');
  const payload = isVideoUrlOrId
    ? { youtube_video_id: val }
    : { youtube_handle: val, youtube_video_id: '' };

  if (lblYtStatus) {
    lblYtStatus.textContent = '再検出中...';
    lblYtStatus.style.color = '#38bdf8';
  }
  try {
    // サーバー側のイベントバッファリセットに合わせてクライアント側のシーケンスも初期化
    ytInitialSynced = false;
    ytLatestSeq = -1;
    await authFetch(`${API_BASE}/api/youtube/reconnect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    setLog(`🔴 YouTube Live 接続先を更新しました: ${val}`);
    setTimeout(pollYouTubeLiveChat, 800);
  } catch (e) {}
}

if (inpYtHandle) {
  inpYtHandle.addEventListener('input', () => { inpYtHandle.dataset.edited = '1'; });
}
if (btnYtReconnect) {
  btnYtReconnect.addEventListener('click', saveAndReconnectYouTube);
}
if (btnYtSave) {
  btnYtSave.addEventListener('click', saveAndReconnectYouTube);
}

startYouTubePolling();
initViewer();
