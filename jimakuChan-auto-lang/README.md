# `jimakuChan-auto-lang/` — 音声認識字幕ちゃん 翻訳言語自動切替 ＆ Chrome内蔵翻訳ブリッジ拡張

[CommentViewer](../CommentViewer/README.md) や [VoiceCommand](../VoiceCommand/README.md) と「音声認識字幕ちゃん (`https://sayonari.github.io/jimakuChan/*`)」を連携し、以下の **3つ** の役割を担う Chrome 拡張機能（Manifest V3）です。

1. **字幕ちゃんの翻訳先言語自動切替（5分タイマー）**: 海外コメントの言語に合わせて字幕ちゃんの第1翻訳言語を自動切替し、5分間外国語コメントがなければ英語 (`en`) に自動復帰。
2. **OBS ブラウザソース向け「Chrome内蔵（高速・無料）翻訳」代行ブリッジ**: OBS のブラウザソース（CEF）自体は `window.Translator` を持たないため、`CommentViewer` からの翻訳リクエスト（`CHROME_TRANSLATE_REQ`）を OBS WebSocket 経由で受け取り、**本物の Google Chrome 上の `window.LanguageDetector` ＆ `window.Translator` で高速・無料に日本語へオンデバイス翻訳**して `CHROME_TRANSLATE_RES` を返却します（外部APIの `429 Too Many Requests` を完全回避）。
3. **OBS 音声コマンド連携（`VoiceCommand`）**: 字幕ちゃんの確定発話を非破壊で取得して `POST http://localhost:8080/api/voice-command/utterance` へ送信し、コマンド一致時は右下バッジ付近に実行結果トーストを表示します。

## ファイル構成と主な責務

```text
jimakuChan-auto-lang/
├── README.md        # 本ドキュメント
├── manifest.json    # Chrome拡張 Manifest V3 定義 (対象URL: sayonari.github.io/jimakuChan/*)
├── content.js       # OBS WebSocket (BroadcastCustomEvent) ＆ server.py (/api/subtitle/lang) ポーリング監視 (~223行)
└── page_bridge.js   # 字幕ちゃん言語セレクト切替・OBS WebSocket接続・Chrome内蔵翻訳ブリッジ (~355行)
```

## 連携フロー

1. OBS ブラウザソース内の [CommentViewer/js/translator-jimaku.js](../CommentViewer/js/translator-jimaku.js) が外国語コメントを受信すると、OBS WebSocket (`BroadcastCustomEvent` / `realm: 'obs-chrome-translate-v1'`) で `CHROME_TRANSLATE_REQ` を送信。
2. Chrome 上で開いている字幕ちゃんタブ内の [page_bridge.js](page_bridge.js) がそれを受信し、Chrome 内蔵の `window.LanguageDetector` と `window.Translator` で日本語へオンデバイス翻訳して `CHROME_TRANSLATE_RES` を即座に返却。
3. 同時に `FOREIGN_COMMENT_LANG` イベントで字幕ちゃんの第1翻訳言語セレクトボックスを該当言語へ切り替え、5分カウントダウンを開始します。

## セットアップ（配布パッケージ利用時）

`CommentViewer` または `VoiceCommand` パッケージに同梱されています。単体では動作しません。

1. Chrome で `chrome://extensions` を開き、右上の「デベロッパーモード」を有効にします。
2. 「パッケージ化されていない拡張機能を読み込む」から、このフォルダを選択します。
3. 音声認識字幕ちゃん（`https://sayonari.github.io/jimakuChan/v2/`）のページを Chrome で開いておきます。サーバーと OBS の起動手順は `CommentViewer/README.md` または `VoiceCommand/README.md` の「セットアップ（配布パッケージ利用時）」を参照してください。
