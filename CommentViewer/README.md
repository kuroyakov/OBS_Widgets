# `CommentViewer/` — Twitch ＆ YouTube Live 統合コメント＆アラートビュワー

Twitch（IRC チャット ＆ EventSub アラート）と YouTube Live（チャット・スパチャ・メンバーシップ）を1つの透過オーバーレイに統合表示し、**外国語コメントの日本語自動翻訳**・**VOICEVOX ずんだもん等による音声読み上げ**・**音声認識字幕ちゃん (jimakuChan) の翻訳言語5分間自動切替**を行うウィジェットです。

## ファイル構成と主な責務

```text
CommentViewer/
├── README.md                # 本ドキュメント
├── index.html               # ステージ領域 (#viewer-stage) と管理パネル (#hud-panel) のHTML構造 (~160行)
├── css/
│   └── style.css            # コメントカード・アラートカード・アニメーション・HUDパネルのスタイル (~390行)
└── js/
    ├── state-settings.js    # DOM参照・設定(settings)の読込/保存・重複ID排除・HキーHUD表示切替 (~155行)
    ├── translator-jimaku.js # Chrome内蔵翻訳(直接＆OBS WebSocket×Chrome拡張ブリッジ)・Webフォールバック・字幕ちゃん連動 (~575行)
    ├── voicevox-tts.js      # VOICEVOX話者リスト取得・読み上げテキスト整形・音声合成再生キュー (~180行)
    ├── feed-renderer.js     # Twitch/YouTubeバッジ生成・コメント/アラートDOM描画・重複耐性テスト発火 (~310行)
    ├── feed-logger.js       # 配信中のコメント・アラート履歴送信 (/api/feed-log) とセッション状態復元 (~80行)
    ├── twitch-client.js     # Twitch IRC ＆ EventSub 接続・全イベント横断重複排除・マルチパケットテスト注入 (~640行)
    └── youtube-poller.js    # YouTube Live ポーリング (/api/youtube/poll)・リロード時過去コメスキップ (~135行)
```

## 各JSモジュールの責務（`index.html` 読み込み順）

1. **[js/state-settings.js](js/state-settings.js)**
   * `API_BASE`、`VOICEVOX_DIRECT_URL`、全DOM要素の参照、設定オブジェクト `settings` の `localStorage` ＆ `server.py` 同期を管理。
   * **表示スタイル/テーマ切替 (`settings.theme`)**: `comic`（スト6風コミック吹き出し）と `default`（スタンダード・ダークグラス）をHUD選択またはURLパラメータ（`?theme=comic` / `?theme=default`）で即座に切替可能。
   * IRC と EventSub の二重受信や再送を防ぐ `markProcessed(id)` セットを管理。
   * `H` キーまたは「隠す」ボタンによる右上管理パネル（`#hud-panel`）の表示/非表示切替（URLパラメータ `?hud=0` で初期非表示）。
2. **[js/translator-jimaku.js](js/translator-jimaku.js)**
   * **多段翻訳パイプライン (`convertCommentToJapanese`)**:
     * **ステージ0-A（ローカル Chrome 内蔵翻訳）**: CommentViewer 自体を Chrome で開いている場合は、直接 `window.Translator` ＆ `window.LanguageDetector` でオンデバイス翻訳（高速・無料・429なし）。
     * **ステージ0-B（OBS ブラウザソース ⇄ OBS WebSocket ⇄ Chrome 拡張ブリッジ）**: CommentViewer を OBS のブラウザソース（CEF）で表示している場合、OBS WebSocket (`BroadcastCustomEvent` / `realm: 'obs-chrome-translate-v1'`) 経由で、裏で開いている Google Chrome の `jimakuChan-auto-lang` 拡張へ翻訳リクエスト（`CHROME_TRANSLATE_REQ`）を送信。本物の Chrome 側の `window.Translator` で高速・無料に日本語化して結果（`CHROME_TRANSLATE_RES`）を受け取ります。
     * **ステージ1〜3（Chrome を開いていない時のフォールバック）**: メモリキャッシュ（200件）＋ 配信スラング辞書 → ブラウザ直接 Google 翻訳（429時は60秒クールダウン） → `POST /api/translate` → MyMemory API。
   * 外国語コメントを翻訳した際、OBS WebSocket `BroadcastCustomEvent` と `POST /api/subtitle/lang` の両方で「音声認識字幕ちゃん」の翻訳先言語を該当言語へ切り替え、5分無発言で英語 (`en`) に自動復帰する `notifyForeignCommentToJimakuChan()`。
3. **[js/voicevox-tts.js](js/voicevox-tts.js)**
   * ローカル VOICEVOX Engine (`127.0.0.1:50021` 直接または `/api/voicevox/*` 経由) から話者一覧を取得し、コメントやアラートを順番に音声合成して再生するキュー (`enqueueTts`, `processTtsQueue`)。
4. **[js/feed-renderer.js](js/feed-renderer.js)**
   * プラットフォーム識別バッジ（🟣 Twitch / 🔴 YouTube）のSVG生成、コメントカード描画 (`handleIncomingChat`)、アラートカード描画 (`handleIncomingAlert`)、および全14種イベントの重複耐性テスト発火 (`runTestTrigger`)。
5. **[js/feed-logger.js](js/feed-logger.js)**
   * 配信開始／終了マーカーに連動してコメント・アラート履歴を `POST /api/feed-log` へ送信し、リロード時は `GET /api/feed-log/status` からセッション状態を復元。
6. **[js/twitch-client.js](js/twitch-client.js)**
   * Twitch IRC WebSocket と Twitch EventSub WebSocket の同時接続および横断的な重複排除（チャネポ・ビッツ・継続サブスクの通常チャット混入防止、および IRC `USERNOTICE` と EventSub の二重発火防止）。
7. **[js/youtube-poller.js](js/youtube-poller.js)**
   * 2秒間隔で `/api/youtube/poll` をポーリング（初回 `since=-1` でリロード前の過去コメント再読み上げを防止）。

## セットアップ（配布パッケージ利用時）

このパッケージは同梱のウィジェットサーバー（Python 3、標準ライブラリのみ）を使います。

1. `twitch_config.example.json` を `twitch_config.json` にコピーし、以下を記入します。
   * `client_id` / `client_secret`: [Twitch 開発者コンソール](https://dev.twitch.tv/console) で作成したアプリの値。アプリの OAuth リダイレクト URL には `http://localhost:8080/api/twitch/callback` を登録してください。
   * `channel_name`: 自分の Twitch チャンネル名
   * `obs_password`: OBS WebSocket のパスワード（OBS の「ツール → WebSocket サーバー設定」で設定したもの）
2. パッケージのルートで `python3 server.py` を実行し、サーバーを起動します（http://localhost:8080 で待ち受けます）。
3. OBS のソースに「ブラウザ」を追加し、URL に `http://localhost:8080/CommentViewer/` を指定します。
4. 初回は CommentViewer の管理パネル（HUD）にある認可ボタンから Twitch 認可を行います。認可が完了するとコメント表示とアラート（EventSub）が有効になります。
5. 任意機能:
   * 読み上げ: VOICEVOX Engine を起動し、`twitch_config.json` の `viewer_settings`（`tts_enabled` / `speaker_id` 等）を調整します。
   * YouTube Live 連携: `youtube_handle`（または `youtube_video_id`）を設定します。
   * 字幕ちゃん連動・Chrome 内蔵翻訳ブリッジ: 同梱の `jimakuChan-auto-lang` 拡張を Chrome に読み込み、字幕ちゃんのページを開いておきます（手順は `jimakuChan-auto-lang/README.md` を参照）。
