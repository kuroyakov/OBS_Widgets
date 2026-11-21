// ========================================================
// ブラウザソース読み込み完了ビーコン (shared/page-ready.js) — Issue #29
// - サーバー主導の逐次リフレッシュで「このページの読み込みが終わった」ことを
//   サーバーへ知らせるための小さな合図。ページの制御には関与しない。
// - window の load 完了後に1回だけ POST する。逐次リフレッシュの
//   セッションが動いていないときはサーバー側で無視される。
// - 送信に失敗してもページの動作には影響しない（例外は握り潰す）。
// ========================================================
(function () {
  'use strict';

  function sendReady() {
    try {
      var base = '';
      try {
        var env = window.OBSWidgetsEnv || {};
        if (typeof env.API_BASE === 'string') base = env.API_BASE;
      } catch (e) {}
      var doFetch = (typeof window.authFetch === 'function') ? window.authFetch : fetch;
      doFetch(base + '/api/launcher/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'page_ready', page: window.location.pathname }),
        keepalive: true
      }).catch(function () {});
    } catch (e) {}
  }

  if (document.readyState === 'complete') {
    sendReady();
  } else {
    window.addEventListener('load', sendReady);
  }
})();
