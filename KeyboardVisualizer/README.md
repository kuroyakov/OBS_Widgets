# `KeyboardVisualizer/` — MIDI 鍵盤ビジュアライザー ＆ OBS 自動ダッキング

Web MIDI API を使用して MIDI キーボードの打鍵をリアルタイムで光らせるとともに、**鍵盤を弾いている間だけ OBS の「BGM」を自動でフェードアウト（ダッキング）し、弾き終えて一定時間経過すると自動でフェードイン復帰させる**ウィジェットです。

## ファイル構成と主な責務

```text
KeyboardVisualizer/
├── README.md          # 本ドキュメント
├── keyboard.html      # Chrome で開く MIDI 入力検知＆OBS制御マスター画面 (~37行)
├── viewer.html        # OBS ブラウザソース (`KeyboardVisualizer`) 用の透過鍵盤表示ビューア (~26行)
├── keyboard.png       # 88鍵盤ベース画像アセット
├── css/
│   ├── keyboard.css   # マスター画面用スタイル（鍵盤発光・ステータスUI）
│   └── viewer.css     # OBS 透過ビューア用スタイル
└── js/
    ├── keyboard.js    # Web MIDI 入力処理・鍵盤ハイライト・OBS BGMダッキング・外部操作ページ連携 (~455行)
    └── viewer.js      # OBS WebSocket / BroadcastChannel 経由で MIDI イベントを受信して鍵盤を発光 (~210行)
```

## 動作の仕組みと外部コントロールパネル連携

* **MIDI 入力と OBS ブラウザソース (`KeyboardVisualizer`) への転送**:
  OBS のブラウザソース（CEF）は Web MIDI API に直接アクセスできない場合があるため、Chrome ブラウザで [keyboard.html](keyboard.html) を開いて MIDI 入力を受け取り、OBS WebSocket の `BroadcastCustomEvent` および `BroadcastChannel('keyboard_visualizer_channel_v1')` で [viewer.html](viewer.html)（OBS ブラウザソース名: `KeyboardVisualizer`）へリアルタイム転送します。
* **外部コントロールパネルからの遠隔操作**:
  * **OBS ソース表示 / 非表示**: 外部コントロールパネルのボタンから、OBS 上のブラウザソース `KeyboardVisualizer` の表示状態をワンタップで切り替えられます。
  * **MIDI 受信受付 ON / OFF（Sキー相当）**: コントロールパネルの「MIDI受信: 稼働中 / 停止中 (S)」ボタン（または `keyboard.html` 上での `S` キー押下）により、MIDI データの受信受付とBGM自動ダッキングを一時停止 (`PAUSED`) / 再開できます。一時停止した際は点灯中の鍵盤が即座にクリアされ、状態は `KEYBOARD_CONTROL` / `KEYBOARD_STATE` イベントで双方向同期されます。

## 鍵盤背景画像について
本リポジトリにはサンプルの鍵盤画像 (`keyboard.example.png`) が同梱されています。
お手持ちのキーボードの実写写真や専用画像を利用したい場合は、本フォルダ内に `keyboard.png` というファイル名で画像を配置してください。`keyboard.png` は `.gitignore` に指定されているため、Git管理外として安全に手元環境でのみ利用されます。

## セットアップ（配布パッケージ利用時）

Python サーバーは不要です（OBS WebSocket に直接接続します）。

1. `shared/env-config.example.local.js` を `shared/env-config.local.js` にコピーし、`OBS_DEFAULT_PASSWORD` に OBS WebSocket のパスワードを記入します（OBS の「ツール → WebSocket サーバー設定」で設定したもの。パスワード認証を使わない場合は不要です）。
2. `keyboard.html` を Google Chrome で開き、MIDI キーボードの入力を許可します（OBS のブラウザソースでは Web MIDI が使えない場合があるため、入力は Chrome 側で受けます）。
3. OBS のソースに「ブラウザ」を追加し、「ローカルファイル」にチェックして `viewer.html` を指定します。
4. 鍵盤の背景画像については「鍵盤背景画像について」の節を参照してください（初期状態では同梱のサンプル画像が表示されます）。
