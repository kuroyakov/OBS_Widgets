# OBS_Widgets 公開仕様書

この文書は、公開リポジトリにおけるアーキテクチャとモジュール間契約を定義します。
実装の詳細ではなく、モジュール同士が守るべき約束（インターフェース・不変条件）を
正本としてまとめます。

## 1. 全体構成

- **バックエンド**: `server.py` をエントリポイントとする Python 製ローカル Web サーバー
  （パッケージは `server/`）。HTTP はループバック `8080`、TLS は `8443` で待ち受けます。
- **ウィジェット**: リポジトリ直下の各ディレクトリが 1 ウィジェットに対応し、
  サーバーが静的ファイルとして配信します。OBS のブラウザソースから
  `http://localhost:8080/<WidgetName>/...` で読み込みます。
- **共通基盤**: `shared/` に全ウィジェット共通の JS（環境設定、OBS 接続、ページ準備合図）を置きます。
- **拡張**: 拡張ディレクトリ機構により、本体に手を入れずに静的ウィジェットと
  サーバーモジュールを追加できます（§4）。

## 2. サーバーの責務と不変条件

- 静的配信: URL の第 1 セグメントでウィジェットを識別し、対応ディレクトリから配信します。
- 機密保護: 設定ファイル（`twitch_config.json` 等）や証明書への直接 HTTP アクセスは
  `403 Forbidden` を返します。
- 状態変更系 POST は、localhost オリジンまたはウィジェットトークン
  （`shared/api-token.js` の仕組み）を要求します。読み取り専用 GET は LAN からも到達可能です。
- PngTuber の入力は設定ファイル（`pngtuber_config.json`）のみです。実行時の
  マイク割当変更のような運用操作は、外部ツールが設定ファイル自体を書き換える
  ことで行い、サーバーはファイルの変更を自動検出して反映します。
- ログ: サーバーイベントは `logs/event-YYYYMMDD.jsonl`（14 日保持）、
  コメント・アラート履歴は `logs/feed-YYYYMMDD.jsonl` に記録します。どちらも git 管理外です。

## 3. 主要 API 契約（本体）

| エンドポイント | 概要 |
| :--- | :--- |
| `GET /api/config` | ローカル設定値（OBS 接続先・チャンネル名・表示設定）の軽量返却 |
| `GET /api/status` (`/api/twitch/status`) | Twitch 認証状態・現在のチャンネル情報・カテゴリプリセット・表示設定の一括取得 |
| `GET /api/twitch/auth` / `GET /api/twitch/callback` | Twitch OAuth 認可フロー（トークンは `twitch_config.json` に保存） |
| `GET /api/voicevox/speakers` (`/api/voicevox/status`) | ローカル VOICEVOX Engine の話者・スタイル一覧 |
| `GET /api/subtitle/lang` / `POST /api/subtitle/lang` | 字幕連動用の翻訳言語とタイマー状態の取得・更新 |
| `GET /api/youtube/poll?since=<seq>` | YouTube Live チャット＆スパチャの差分取得 |
| `GET /api/pngtuber/stream` | PNGTuber 向け音声レベルの SSE 配信 |
| `GET /api/pngtuber/level` | PNGTuber 向け音声レベルの単発取得 |
| `GET /api/pngtuber/config` | PNGTuber の解決済み設定（画像 URL・しきい値・瞬き設定） |
| `GET /api/pngtuber/image/<slot>` | 設定で指定されたローカル画像の配信（許可スロットのみ） |
| `POST /api/twitch/save-config` | `twitch_config.json` への設定マージ保存 |
| `POST /api/twitch/set-category` (`/api/twitch/set-title`) | Twitch Helix API による配信カテゴリ・タイトル更新 |
| `POST /api/twitch/eventsub/subscribe` | EventSub WebSocket セッションへのイベント購読登録 |
| `POST /api/translate` | サーバーサイド翻訳（Google Translate GTX + MyMemory フォールバック） |
| `POST /api/youtube/reconnect` / `POST /api/youtube/test` | YouTube 接続先の保存・再検出 / テスト注入 |
| `POST /api/voicevox/tts` (`/api/voicevox/synthesis`) | VOICEVOX WAV 音声合成 |

拡張モジュールが有効な場合、上記に加えてモジュール固有のエンドポイントが追加されます。

## 4. 拡張機構の契約

- 拡張ディレクトリは環境変数 `OBS_WIDGETS_EXTENSIONS_DIR`（絶対パス）で指定します。
  未設定または空文字の場合は無効です。環境変数が未設定のときに限り、リポジトリ直下の
  `extensions/` ディレクトリが存在すればそれを使います。
- **静的マウント**: 拡張ディレクトリ直下のディレクトリが、第 1 セグメント名で
  マウントされます。本体側に同名のファイル、または中身のあるディレクトリが存在する
  場合は本体側が優先され、拡張側は無効化されます（起動時に警告を出します）。
  本体側に空ディレクトリしか無い場合は衝突とみなさず、拡張側をマウントします。
  ドット始まりの名前と予約名（`server_modules` 等）はマウント対象外です。
- **任意モジュール**: 本体 `server/` パッケージに同名モジュールが無い場合に限り、
  `server_modules/<name>.py` が読み込まれます。拡張モジュールは本体モジュールを
  絶対 import（`from server.X import ...`）で参照できます。
- 拡張側の設定ファイルは本体側の解決規則（`server.config` の `BASE_DIR`）に従い、
  拡張ディレクトリには置きません。

## 5. クライアント共通契約（shared/）

- `shared/env-config.js`: 環境定数の既定値。個別環境の上書きは git 管理外の
  `shared/env-config.local.js` で行います。
- `shared/api-token.js`: 状態変更系 API 用トークンの付与。
- `shared/page-ready.js`: ページの読み込み完了をサーバーへ知らせる合図。
  サーバーはこの合図を 200 で受け流し、拡張モジュールが順次再読み込み等に利用できます。
- `shared/obs-client.js`: OBS WebSocket への安全な接続ラッパー。

## 6. 配布パッケージ

- パッケージ定義は `tools/build_package.py` が持ち、タグ `<package>-vX.Y.Z` の
  push で release ワークフローが ZIP をビルドして Release に添付します。
- 同梱ファイルは git 管理対象のみです。個人設定や第三者素材などの git 管理外
  ファイルは構造的にパッケージへ入りません。
- ZIP 化の前に個人情報スキャン（`tools/privacy_scan.py`）を例外なしで適用し、
  ヒットしたらビルドを中止します。
