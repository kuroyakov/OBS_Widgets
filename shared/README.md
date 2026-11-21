# `shared/` — 全 Widget 共通基盤モジュール

複数の Widget（`CommentViewer`, `Ticker`, `WebCanvas`, `KeyboardVisualizer`, `PngTuber` 等）で共通利用する環境設定および OBS WebSocket クライアントラッパーを格納するフォルダです。

## ファイル構成と主な責務

```text
shared/
├── README.md        # 本ドキュメント
├── env-config.js    # 接続先ホスト自動判別・OBSパスワード・シーン名・ソース名・BroadcastChannel名の一元定義
└── obs-client.js    # リスナー重複登録防止・再接続タイマー排他制御・アドレス自動フォールバック付き OBS WebSocket ラッパー
```

## 主なメリット

1. **固定IPハードコードの撤廃**: `WS_HOST` が `location.hostname || 'localhost'` を自動解決するため、Mac ローカル（OBS ブラウザソース）から開いても iPad Safari（LAN 経由）から開いても、Mac の IP アドレス変更に影響されずそのまま接続できます。
2. **OBS パスワード・シーン名の一元管理**: パスワードやシーン名を変更したい場合、[env-config.js](env-config.js) の1箇所を書き換えるだけで全 Widget に反映されます。
3. **ゾンビ接続・メモリリーク防止**: [obs-client.js](obs-client.js) の `createManagedObsClient()` が再接続時の `removeAllListeners()`・`disconnect()` および単一タイマー排他制御を保証します。
