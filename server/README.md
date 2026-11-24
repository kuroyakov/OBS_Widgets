# `server/` — OBS Widgets ローカル Web サーバー パッケージ

ルートディレクトリの [server.py](../server.py) から呼び出されるバックエンドパッケージです（HTTP `8080` ループバック / TLS `8443`）。
各機能ごとにモジュールが分離されており、特定の機能を改修する際に他の無関係なコードを読み込む必要がありません。

## ファイル構成と主な責務

```text
server/
├── __init__.py       # パッケージ初期化・構成概要
├── config.py         # 定数 (PORT, TLS_PORT, BASE_DIR, SSL_CONTEXT, SSL_CONTEXT_INFO)・カテゴリ定義・twitch_config.json 読み書き
├── obs_ws.py         # 最小 OBS WebSocket (v5) クライアント
├── twitch_api.py     # Twitch OAuth トークン検証・自動リフレッシュ・Helix API 呼び出し・EventSub 登録
├── translator.py     # 日本語判定・Google翻訳/MyMemory翻訳・字幕連動用タイマーステート
├── voicevox.py       # ローカル VOICEVOX Engine (127.0.0.1:50021) 話者一覧取得・WAV音声合成
├── youtube_chat.py   # YouTube Live チャット・スパチャ取得 (InnerTube方式・重複排除・テスト注入)
├── event_log.py      # サーバー常設イベントログ (logs/event-YYYYMMDD.jsonl、14日保持)
├── feed_log.py       # コメント・アラート履歴ログ (logs/feed-YYYYMMDD.jsonl)
├── pngtuber_audio.py # PNGTuber 用 OBS 音声レベル計測・設定/画像パス解決 (pngtuber_config.json 駆動)
├── extensions.py     # 拡張ディレクトリ機構 (静的マウント・任意モジュールの読み込み)
├── test_extensions.py # extensions.py の単体テスト
├── test_pngtuber_settings.py # PNGTuber 設定保存・画像アップロード API のテスト
├── test_pngtuber_mic_assignment_hook.py # PNGTuber マイク割当拡張フックのテスト
└── handler.py        # UnifiedWidgetHandler (静的ファイル配信・403保護・GET/POST API ルーティング)
```

## 拡張機構

環境変数 `OBS_WIDGETS_EXTENSIONS_DIR` で拡張ディレクトリを指定すると、その直下の
ディレクトリが静的ファイルとしてマウントされ、`server_modules/` 内の Python
モジュールが任意モジュールとして読み込まれます（本体側に同名モジュールがある場合は
本体側が優先）。環境変数が未設定の場合は、リポジトリ直下の `extensions/` があれば
それを使います。契約の詳細は [SPEC.md](../SPEC.md) を参照してください。

## モジュール別詳細

* **[config.py](config.py)**: `PORT`（既定 `8080`、環境変数 `OBS_WIDGETS_PORT` で変更可）、`TLS_PORT`（既定 `8443`、環境変数 `OBS_WIDGETS_TLS_PORT` で変更可。`1〜65535` 範囲外・非数値・TLS 有効時の HTTP との重複時は起動失敗）、`CONFIG_FILE` (`twitch_config.json`)、`REQUIRED_SCOPES`、`CATEGORY_PRESETS`、設定の読み書きを管理。また `build_outbound_ssl_context()` でサーバー発 HTTPS 用 `SSL_CONTEXT` を構築します（既定ストア → `certifi` → OS 候補パスの順でフォールバックし、証明書検証は常時有効）。カテゴリプリセットはコードに組み込まず、`twitch_config.json` の `category_presets` キーで定義します（サーバー起動時に読み込み）。
* **[twitch_api.py](twitch_api.py)**: OAuth トークン検証・自動リフレッシュ、Helix API 呼び出し、EventSub イベント購読登録。
* **[translator.py](translator.py)**: 外国語コメントの日本語翻訳（LRUキャッシュ・429クールダウン制御付き）および字幕連動用タイマー管理。
* **[voicevox.py](voicevox.py)**: VOICEVOX Engine (`127.0.0.1:50021`) の話者一覧取得・WAV 音声合成。
* **[youtube_chat.py](youtube_chat.py)**: InnerTube 方式による YouTube Live チャット・スパチャ・メンバーシップ監視およびテスト注入 (`inject_test_innertube_event`)。非公式 API のため仕様変更で動作しなくなる場合があります。
* **[pngtuber_audio.py](pngtuber_audio.py)**: `PngTuber/` 向けの OBS 音声レベル計測・SSE ストリーム配信と設定管理（`pngtuber_config.json` → `pngtuber_config.example.json` → 既定値の順に読み込み）。外部 CLI/ライブラリを使わず、[obs_ws.py](obs_ws.py) 経由で obs-websocket (v5) の `InputVolumeMeters` を購読して対象ソース（`obs_input_name` 指定またはマイク入力自動選択）のレベル値を保持・通知します（録音・保存は行いません）。`GET /api/pngtuber/stream` (SSE) で即時プッシュし、`GET /api/pngtuber/level` の単発取得も維持します。ローカル画像パスはクライアントへ生パスを出さず `/api/pngtuber/image/<slot>` 経由で配信します。入力は設定ファイルのみで、ファイルの変更は自動検出して反映します（実行時の割当変更は外部ツールが設定を書き換える形で行います）。
* **[extensions.py](extensions.py)**: 拡張ディレクトリの解決、静的マウントの発見、任意モジュールの読み込み、任意モジュール不在時の警告補助を提供します。
* **[handler.py](handler.py)**: `/api/*` のルーティングと静的ファイル配信（機密ファイルへの直接 HTTP アクセスは `403 Forbidden` で保護）。`/api/status` では `tls_store_ok` をトップレベルに、詳細 `tls_store` を `can_write` 時のみ返します。任意モジュールが不在の場合、関連 API は無効化されます（page_ready の合図だけは 200 で受け流します）。
