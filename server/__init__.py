"""
OBS Widgets バックエンドサーバーパッケージ
各機能モジュール:
- config.py       : ポート・パス・SSL設定・twitch_config.json の読み書き・プリセット定義
- twitch_api.py   : Twitch OAuth トークン検証・自動リフレッシュ・Helix API 呼び出し・EventSub 登録
- translator.py   : 外国語コメントの日本語翻訳・音声認識字幕ちゃん (jimakuChan) 翻訳言語ステート管理
- voicevox.py     : ローカル VOICEVOX Engine 話者一覧取得・WAV音声合成
- youtube_chat.py : YouTube Live チャット＆スパチャ直接取得マネージャー (InnerTube 方式)
- obs_ws.py       : 最小 OBS WebSocket (v5) クライアント (ブラウザソース再読み込み・PNGTuber 音声メーター監視用)
- event_log.py    : サーバー常設イベントログ (logs/event-YYYYMMDD.jsonl、14日保持)
- feed_log.py     : コメント・アラート履歴ログ (logs/feed-YYYYMMDD.jsonl、無期限、UC-09)
- pngtuber_audio.py : PNGTuber 用 OBS 音声レベル計測・設定管理
- extensions.py   : 拡張ディレクトリ機構 (OBS_WIDGETS_EXTENSIONS_DIR / extensions/、静的マウント・任意モジュール読み込み)
- handler.py      : HTTP リクエストハンドラ (UnifiedWidgetHandler)

拡張ディレクトリ側 (extensions/server_modules/) にも任意モジュールを配置でき、
同名モジュールがパッケージ内に無い場合に読み込まれる (詳細は SPEC.md の拡張機構)。
"""
