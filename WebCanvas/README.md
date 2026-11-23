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

1. `shared/env-config.example.local.js` を `shared/env-config.local.js` にコピーし、`OBS_DEFAULT_PASSWORD` に OBS WebSocket のパスワードを記入します（OBS の「ツール → WebSocket サーバー設定」で設定したもの）。
   * **セキュリティ注意**: TLS（ポート `8443`）および `/obs-ws` トンネルを利用する場合は、LAN 内の未認証クライアントによる操作を防ぐため **OBS 側の WebSocket サーバー設定でパスワード認証を必ず有効にしてください**。また、ポート `8443` をルーターのポート開放等でインターネット側へ公開せず、信頼された自宅 LAN 内でのみ使用してください。
2. **iPad など別端末から描画する場合（推奨）**:
   - iPad の Safari など別端末から開くには HTTPS（ポート `8443`）が必要です。サーバーはパッケージ直下に `certs/tls-cert.pem` と `certs/tls-key.pem` が存在するときだけポート `8443`（HTTPS）を起動します。事前にいずれかの方法で証明書を作成してください：
     * **`mkcert` を使う場合（推奨）**:
       ```bash
       mkdir -p certs
       mkcert -cert-file certs/tls-cert.pem -key-file certs/tls-key.pem localhost 127.0.0.1 <PCのホスト名>.local <PCのIPアドレス>
       ```
     * **`openssl` で自己署名証明書を作る場合**:
       ```bash
       mkdir -p certs
       openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
         -keyout certs/tls-key.pem -out certs/tls-cert.pem \
         -subj "/CN=localhost"
       ```
   - 証明書を配置したら、パッケージ直下で `python3 server.py`（macOS では `WebCanvas/start.command` でも可）を起動し、上記「使い方・機能仕様」のとおり OBS 側で `http://localhost:8080/WebCanvas/overlay.html`、iPad 側で `https://<PCのホスト名>.local:8443/WebCanvas/pad.html` を開きます。
3. **同一 PC 内のブラウザのみで描画する場合**: サーバーを起動せず、OBS のブラウザソースでローカルファイルの `WebCanvas/overlay.html`（幅 1920・高さ 1080）、ブラウザで `WebCanvas/pad.html` を直接開くことでも動作します。
