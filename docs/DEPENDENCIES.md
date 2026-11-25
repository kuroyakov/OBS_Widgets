# 外部依存・通信先・標準ライブラリ台帳 (SBOM)

本リポジトリは原則としてフルスクラッチ（Python 標準ライブラリ + バニラ HTML/CSS/JS）で実装されており、`package.json` や `requirements.txt` を持ちません。  
この台帳は、**Python 本体・標準ライブラリの CVE**、**外部ライブラリの脆弱性**、**外部通信先でのセキュリティ事案・障害**が発生した際に、影響範囲とユーザーへの注意喚起対象を即座に特定するための逆引きドキュメントです。

機械可読な CycloneDX 形式の SBOM はリポジトリ直下の [sbom.cdx.json](../sbom.cdx.json) に出力しています。

## 運用と鮮度チェック

コード変更で新しい外部 CDN、Python パッケージ、外部通信先ドメイン、またはセキュリティ上重要な標準ライブラリが追加された場合、CI（[.github/workflows/checks.yml](../.github/workflows/checks.yml)）で検知します。

```bash
# 整合性チェック（sbom.cdx.json と本ドキュメントの記載漏れを検証）
python3 tools/sbom_audit.py --check

# sbom.cdx.json を最新コードから再生成
python3 tools/sbom_audit.py --generate-sbom
```

---

## 1. 外部ライブラリ・SDK・Web フォント

### フロントエンド（ランタイム）

| コンポーネント | バージョン | 取得元 / 参照 | 整合性保護 | 利用ウィジェット | 脆弱性・事案時の対応 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **obs-websocket-js** | `5.0.8` | `https://cdn.jsdelivr.net/npm/obs-websocket-js@5.0.8/dist/obs-ws.global.js` | SRI (`sha384-lY77rD++w3IGkFCrXckuC7IRv29rr92Bx9Ofo6vqsf4y3mFyPzWbHmNkCo5I3hbe`) | `CommentViewer/index.html`, `Ticker/info.html`, `WebCanvas/overlay.html`, `WebCanvas/pad.html`, `KeyboardVisualizer/keyboard.html`, `KeyboardVisualizer/viewer.html` | CDN 改ざん時は SRI により実行が拒否される。ライブラリ脆弱性時はバージョン更新＋SRI 再計算。 |
| **OneSDK (OneComme)** | わんコメ本体準拠 | `../../preset/__origin/js/onesdk.js`（ローカル相対参照・未同梱） | ローカル配置 | `EndRoll/index.html` | わんコメ本体のアップデートに追従。 |
| **Google Fonts** | — | `fonts.googleapis.com` / `fonts.gstatic.com` | — | `Clock/Date.html`, `Ticker/info.html`, `EndRoll/index.html` | フォント配信障害時はシステムフォントへフォールバック。 |

### Python サーバー（ランタイム）

| パッケージ | バージョン | 必須 / 任意 | 利用箇所 | 備考・脆弱性時の対応 |
| :--- | :--- | :--- | :--- | :--- |
| **certifi** | 未固定 | **任意**（フォールバック） | `server/config.py` | システム CA ストアが使えない環境向けの動的フォールバック。環境側に導入されている場合のみ参照される。 |

---

## 2. 外部通信先エンドポイント（セキュリティ事案・障害時の注意喚起用）

外部サービスで侵害・情報漏洩・障害・仕様変更が報告された際、どのウィジェットに影響があり、ユーザーに何を案内すべきかをまとめています。

| ドメイン | 用途 | 送信データ・認証情報の有無 | 利用箇所 | 事案発生時の注意喚起・暫定回避策 |
| :--- | :--- | :--- | :--- | :--- |
| `id.twitch.tv` | Twitch OAuth 認証・トークン検証/更新 | **Client ID / Secret / OAuth トークン** | `server/handler.py`, `server/twitch_api.py` | 事案時はトークン失効・再認証を案内。 |
| `api.twitch.tv` | Twitch Helix API（配信情報・報酬・ユーザー取得等） | **OAuth Bearer Token** | `server/twitch_api.py` | トークン漏洩疑い時は Twitch 側で接続解除＋再認可を案内。 |
| `eventsub.wss.twitch.tv` | Twitch EventSub WebSocket | **OAuth Bearer Token** | `CommentViewer/js/twitch-client.js` | 障害時はチャット表示・アラート連動が停止する旨を案内。 |
| `irc-ws.chat.twitch.tv` | Twitch IRC over WebSocket（匿名/認証チャット受信） | チャンネル名（任意でトークン） | `CommentViewer/js/twitch-client.js`, `StampAlert_Twitch/js/app.js` | 障害時はコメント/スタンプアラート受信が停止。 |
| `static-cdn.jtvnw.net` | Twitch エモート・バッジ画像取得 | 画像 URL 参照のみ（認証なし） | `CommentViewer/js/feed-renderer.js`, `StampAlert_Twitch/js/app.js` | 障害時はエモート画像が表示されないのみ。 |
| `www.youtube.com` | YouTube Live チャット取得（スクレイピング / InnerTube） | 配信 URL / チャンネルハンドル（認証なし） | `server/youtube_chat.py` | YouTube 仕様変更時はチャット取得不可。Twitch 単独運用への切替を案内。 |
| `translate.googleapis.com` | 非公式 Google 翻訳エンドポイント | **チャット本文・発話テキスト**（認証なし） | `server/translator.py`, `CommentViewer/js/translator-jimaku.js`, `jimakuChan-auto-lang/manifest.json`, `jimakuChan-auto-lang/page_bridge.js` | 機密情報を含むテキストを流さないよう注意喚起。障害時は翻訳機能をオフにする。 |
| `api.mymemory.translated.net` | MyMemory 翻訳 API（フォールバック） | **チャット本文**（認証なし） | `server/translator.py`, `CommentViewer/js/translator-jimaku.js` | 第三者公開翻訳 DB に送信される点に留意。不要時は翻訳機能オフを案内。 |
| `decapi.me` | Twitch ユーザーアバター URL 取得 | Twitch ログイン名のみ（認証なし） | `CommentViewer/js/feed-renderer.js` | 外部サードパーティ API。事案・停止時はアバター表示がフォールバックアイコンになる。 |
| `sayonari.github.io` | 音声認識字幕ちゃん (v2) ホスト | Chrome 拡張のコンテンツスクリプト注入先 | `jimakuChan-auto-lang/manifest.json`, `content.js`, `page_bridge.js` | 字幕ちゃん側の DOM 変更やドメイン事案時は拡張機能を無効化するよう案内。 |
| `www.sayonari.com` | 音声認識字幕ちゃん (v1) ホスト | Chrome 拡張のコンテンツスクリプト注入先 | `jimakuChan-auto-lang/manifest.json`, `content.js`, `page_bridge.js` | 同上。 |
| `cdn.jsdelivr.net` | `obs-websocket-js` の配信元 | スクリプト取得のみ（SRI 付き） | 各ウィジェット HTML（上記参照） | CDN 事案時は SRI により自動ブロック。必要に応じローカル同梱へ切替。 |
| `fonts.googleapis.com` / `fonts.gstatic.com` | Google Fonts 配信 | スタイル/フォント取得のみ | `Clock/Date.html`, `Ticker/info.html`, `EndRoll/index.html` | 配信障害時はブラウザの標準フォントへ自動フォールバック。 |
| `images.unsplash.com` | デモ・プレビュー用サンプル画像 | 画像 URL 参照のみ | `CommentViewer/js/feed-renderer.js`, `server/youtube_chat.py` | テスト/デモ表示時のみ使用（本番配信データには影響なし）。 |

---

## 3. Python 本体・標準ライブラリのセキュリティ監視対象

Python 本体に脆弱性（CVE）が公表された際、以下のモジュールに関連するものは本リポジトリへの影響を確認し、Python バージョンアップ推奨またはコード修正のフックとします。

| 標準モジュール | 主なリスク領域 | 利用ファイル |
| :--- | :--- | :--- |
| `http` (`http.server`, `http.client`) | HTTP リクエストスマグリング、ヘッダインジェクション、DoS | `server.py`, `server/handler.py` |
| `ssl` | 証明書検証バイパス、TLS ダウングレード、メモリリーク | `server.py`, `server/config.py`, `server/handler.py` |
| `urllib` (`urllib.request`, `urllib.parse`) | SSRF、オープンリダイレクト、URL パース不整合 | `server/extensions.py`, `server/handler.py`, `server/pngtuber_audio.py`, `server/translator.py`, `server/twitch_api.py`, `server/voicevox.py`, `server/youtube_chat.py`, `tools/e2e_smoke.py` |
| `socket` | ネットワーク境界、バインド制御、タイムアウト | `server.py`, `server/handler.py`, `server/obs_ws.py`, `tools/e2e_smoke.py` |
| `ipaddress` | プライベート IP / SSRF 判定ロジック | `server/handler.py`, `server/voicevox.py` |
| `subprocess` | コマンドインジェクション、PATH 解決 | `server.py`, `tools/build_package.py`, `tools/e2e_smoke.py`, `tools/sbom_audit.py` |
| `zipfile` / `zlib` | Zip Slip（パストラバーサル）、Zip Bomb | `tools/build_package.py`, `tools/e2e_smoke.py`, `twitch-audio-panner/generate_icons.py` |
| `tempfile` / `shutil` | 一時ファイル競合、シンボリックリンク攻撃 | `tools/build_package.py`, `tools/e2e_smoke.py` |
| `hashlib` / `hmac` / `secrets` | 認証トークン生成、WebSocket 署名・定数時間比較 | `server/config.py`, `server/handler.py`, `server/obs_ws.py`, `tools/build_package.py`, `tools/e2e_smoke.py` |
| `html` | XSS エスケープ処理 | `server/handler.py` |
| `json` | 深さ制限・巨大ペイロードによる DoS | `server/*.py`, `tools/e2e_smoke.py`, `tools/privacy_scan.py`, `tools/sbom_audit.py` |

---

## 4. 開発・CI 依存（配布パッケージには含まれない）

| 種別 | 名前 | バージョン / ピン留め | 用途 | 定義ファイル |
| :--- | :--- | :--- | :--- | :--- |
| PyPI (CI) | `playwright` | `1.65.2` | リリース ZIP のスモーク E2E テスト | `.github/workflows/release.yml`, `tools/e2e_smoke.py` |
| PyPI (CI) | `zizmor` | `1.32.0` | GitHub Actions ワークフロー静的セキュリティ監査 | `.github/workflows/checks.yml` |
| GitHub Actions | `actions/checkout` | `v7.0.1` (`3d3c42e5aac5ba805825da76410c181273ba90b1`) | リポジトリチェックアウト | `.github/workflows/checks.yml`, `.github/workflows/release.yml`, `.github/workflows/sbom-scan.yml` |
| GitHub Actions | `actions/setup-node` | `v7.1.0` (`949feb2413d6458794dcd2491c4babbbce0c15c1`) | `node --check` 用の Node 20 セットアップ | `.github/workflows/checks.yml` |
| GitHub Actions | `actions/upload-artifact` | `v7.0.2` (`cf430e030ddbb5b0abf93d22962f4752f3646cd9`) | ビルド成果物・E2E 失敗ログ保存 | `.github/workflows/release.yml` |
| GitHub Actions | `aquasecurity/trivy-action` | `v0.36.0` (`ed142fd0673e97e23eac54620cfb913e5ce36c25`) | SBOM (`sbom.cdx.json`) の週次 CVE スキャン | `.github/workflows/sbom-scan.yml` |
