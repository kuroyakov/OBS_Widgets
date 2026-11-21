# `StampAlert_Twitch/` — Twitch スタンプ カットインアラート

Twitch チャット (`wss://irc-ws.chat.twitch.tv:443`) を直接監視し、コメントに Twitch スタンプが含まれていた際に画面左端からポップなカットイン演出でスタンプを表示するオーバーレイウィジェットです。

## ファイル構成と主な責務

```text
StampAlert_Twitch/
├── README.md          # 本ドキュメント
├── stamp-alert.html   # OBS ブラウザソース用ステージ画面 (~22行)
├── css/
│   └── style.css      # スタンプサイズ・停止位置(--stop-left)・カットイン/フェードアウトアニメ (~140行)
└── js/
    └── app.js         # Twitch IRC WebSocket接続・emotesタグ解析・スタンプ順次ポップアップ描画 (~235行)
```

## カスタマイズ方法

* 対象チャンネルの変更や同時表示数の調整は [js/app.js](js/app.js) 冒頭の `CONFIG`（または URL パラメータ `?channel=your_channel`）で行えます。
* 表示サイズや左端からの距離は [css/style.css](css/style.css) の `:root` 変数で調整できます。

## セットアップ（配布パッケージ利用時）

1. OBS のソースに「ブラウザ」を追加し、「ローカルファイル」にチェックして `stamp-alert.html` を指定します。
2. 対象チャンネルは次のいずれかで指定します。
   * URL パラメータ: `stamp-alert.html?channel=your_channel`
   * 設定ファイル: `shared/env-config.example.local.js` を `shared/env-config.local.js` にコピーし、`TWITCH_CHANNEL` に自分のチャンネル名を記入する
3. 匿名接続でチャットを読むため、Twitch の認証情報は不要です。
