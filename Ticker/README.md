# `Ticker/` — 配信情報スクロールティッカー

OBS の配信画面上部・下部にお知らせテキストをスムーズにスクロール表示するティッカーウィジェットです。
外部の操作ページ（OBS カスタムドック等）から、リアルタイムに文字・速度・シーン連動定型文を変更できます。

## ファイル構成と主な責務

```text
Ticker/
├── README.md            # 本ドキュメント
├── info.html            # OBS ブラウザソース用ティッカー表示画面 (~35行)
├── css/
│   └── info.css         # ティッカー表示・スクロールアニメーション用スタイル (~130行)
└── js/
    └── info.js          # ティッカー文字列生成・シームレススクロール計算・OBS WebSocket/BroadcastChannel/API同期 (~240行)
```

## 同期の仕組み

* **OBS WebSocket (`BroadcastCustomEvent` / `shared/obs-client.js`)**: iPad の Safari や OBS ミニドック等の操作ページから変更した場合、OBS WebSocket (`realm: 'obs-stream-ticker-v1'`) を通じて Mac 側の OBS ブラウザソース ([info.html](info.html)) に即座に反映されます。
* **サーバー永続化同期 (`GET /api/config`)**: iPad 側で変更した最新ティッカー文字列 (`current_ticker`)・速度 (`ticker_speed`)・シーン別定型文 (`ticker_presets`) をサーバー側 (`twitch_config.json`) 経由でも定期同期します。
* **`BroadcastChannel` ＆ `storage` イベント**: 同一 Mac 内のブラウザ・カスタムドック間では `BroadcastChannel('stream_ticker_broadcast_v1')` と `storage` イベントでも即時同期されます。

## セットアップ（配布パッケージ利用時）

1. OBS のソースに「ブラウザ」を追加し、「ローカルファイル」にチェックして `info.html` を指定します。この状態でもティッカーは表示されます（文言はページ内の初期値／ローカル保存分）。
2. OBS WebSocket 経由で他の画面と同期する場合は、`shared/env-config.example.local.js` を `shared/env-config.local.js` にコピーし、`OBS_DEFAULT_PASSWORD` に OBS WebSocket のパスワードを記入します。
3. サーバー経由の同期（`/api/config`）は任意です。サーバーが無くても BroadcastChannel と localStorage による同期で動作します。
