# `WebCanvas/` — スプラ解説・画面注記ペン (Telestrator)

iPad の Safari から OBS の映像ソースをキャプチャしながら、Apple Pencil や指でペン・矢印・四角・楕円を描き込み、OBS の透過ブラウザソース (`overlay.html`) にリアルタイムで重ね合わせる注記ツールです。

## ファイル構成と主な責務

```text
WebCanvas/
├── README.md          # 本ドキュメント
├── pad.html           # iPad 操作用キャンバス＆ツールバー画面（映像のみ絞り込み・★お気に入りボタン付き）
├── overlay.html       # OBS ブラウザソース用透過キャンバス画面 (1920x1080)
├── start.command      # サーバー起動ラッパー（server.py を直接起動）
├── css/
│   ├── pad.css        # iPad ツールバー・カラーパレット・16:9キャンバス枠スタイル
│   └── overlay.css    # OBS 透過オーバーレイ用スタイル
└── js/
    ├── shapes.js      # 矢印(先端突き抜け防止)・矩形・楕円の共通Canvas描画関数
    ├── pad.js         # iPad側タッチ/Apple Pencil入力・映像ソース自動絞り込み・お気に入り管理・OBSスクショ同期
    └── overlay.js     # OBS側 BroadcastCustomEvent 受信＆リアルタイム描画 (shared/obs-client.js 使用)
```

## 使い方・機能仕様

1. **OBS 側設定**:「ブラウザ」ソースを追加し、URL に `http://localhost:8080/WebCanvas/overlay.html`、幅 `1920`・高さ `1080` を指定します。
2. **iPad 側操作**: `https://<Macのホスト名>.local:8443/WebCanvas/pad.html`（または `https://<MacのIP>:8443/WebCanvas/pad.html`）を開きます（LAN 経由の接続には TLS ポート `8443` / HTTPS が必要です）。
   * **スマート映像ソース選択**: デフォルトで **`🎯 映像のみ`** フィルタが有効になっており、BGM・マイク・ブラウザオーバーレイ・PNGTuberアバター（`PNGTUBER_SOURCE_NAME` で指定したソース）を除外して、ゲーム画面等の映像キャプチャソース（`🎮`）だけをプルダウンに表示します。
   * **お気に入りピン留め (`☆` / `★`)**: よく使うソースを選んで `☆` をタップすると `★` お気に入りとして `localStorage` に保存され、次回以降も常にプルダウン最上部に固定表示されます（`🎯 映像のみ` ボタンをタップすると `📋 全ソース` 表示へいつでも切り替えられます）。
   * **描画時の背景自動ロック**: 画面上に何も描かれていない時は1秒おきに選択ソースのスクリーンショットを自動取得して背景に表示します。ペンや矢印を1画描くと自動的に背景更新が「🔒 ロック」され、静止画の上で解説を行えます。「🗑️ 全消去」すると再び自動同期に戻ります。

## セットアップ（配布パッケージ利用時）

Python サーバーは不要です（OBS WebSocket に直接接続します）。

1. `shared/env-config.example.local.js` を `shared/env-config.local.js` にコピーし、`OBS_DEFAULT_PASSWORD` に OBS WebSocket のパスワードを記入します（OBS の「ツール → WebSocket サーバー設定」で設定したもの。パスワード認証を使わない場合は不要です）。
2. OBS のソースに「ブラウザ」を追加し、「ローカルファイル」にチェックして `overlay.html` を指定します（幅 1920・高さ 1080）。
3. 描画側は `pad.html` をタブレット等のブラウザで開きます。OBS と同じネットワークから OBS の PC へ接続できる必要があります。
