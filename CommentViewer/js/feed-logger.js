/* ========================================================
 * コメント・アラート履歴の記録 (UC-09 / Issue #133)
 *
 * CommentViewer が受信したコメント・アラート・配信マーカー・
 * 削除マーカーを、ウィジェットサーバーの POST /api/feed-log へ
 * 送り、サーバー側の logs/feed-YYYYMMDD.jsonl に保持する。
 *
 * - 記録するのは配信中のみ (stream.online 〜 stream.offline)。
 *   ゲートはここ (送信側) とサーバー側の二重で持つ
 * - 配信中のリロード対策として、起動時にサーバー側のセッション状態
 *   (GET /api/feed-log/status) を取得し、開いていれば記録を再開する
 *   (Issue #179)。サーバー側も再起動時にログ末尾から状態を復元する
 * - 記録の失敗で表示・読み上げを妨げない (fire-and-forget、再送なし)
 * - 重複排除はサーバー側がレコードの id で行う。ここでは既存の
 *   messageId / alertId をそのまま id として渡す
 * ======================================================== */
(() => {
  'use strict';

  let streamLive = false;
  // 起動時の状態復元より先にマーカーを受けたら、復元結果で上書きしない
  let markerSeen = false;

  function send(record) {
    // 配信マーカーはゲート外でも送る (ゲートを開閉する側なので)
    if (record.kind !== 'stream' && !streamLive) return;
    try {
      authFetch(`${API_BASE}/api/feed-log`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(record)
      }).catch(() => {});
    } catch (e) {}
  }

  window.FeedLog = {
    record(rec) {
      if (rec && typeof rec.kind === 'string') send(rec);
    },
    streamStarted(info) {
      markerSeen = true;
      streamLive = true;
      send(Object.assign({ kind: 'stream', event: 'start' }, info || {}));
    },
    streamEnded() {
      markerSeen = true;
      send({ kind: 'stream', event: 'end' });
      streamLive = false;
    },
    isLive() {
      return streamLive;
    }
  };

  // 配信中のリロードで stream.online を受け直せないため、サーバー側の
  // セッション状態から記録状態を復元する (Issue #179)。
  // サーバー起動直後のレースに備えて数回リトライし、取得できない場合はマーカー受信を待つ。
  function restoreFeedStatus(attempt = 0) {
    if (markerSeen) return;
    try {
      authFetch(`${API_BASE}/api/feed-log/status`, { cache: 'no-store' })
        .then(r => (r && r.ok) ? r.json() : null)
        .then(data => {
          if (markerSeen) return;
          if (data) {
            if (data.session_open) streamLive = true;
            return;
          }
          if (attempt < 4) setTimeout(() => restoreFeedStatus(attempt + 1), 1500);
        })
        .catch(() => {
          if (!markerSeen && attempt < 4) {
            setTimeout(() => restoreFeedStatus(attempt + 1), 1500);
          }
        });
    } catch (e) {}
  }
  restoreFeedStatus();
})();
