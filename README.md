# OBS_Widgets

配信（Twitch / YouTube Live）で使う OBS Studio 向けブラウザソース・オーバーレイと、ローカル統合バックエンドサーバーのセットです。個人開発のオープンソースプロジェクトとして公開しています。

## 含まれるもの

| 名前 | 内容 |
| :--- | :--- |
| **CommentViewer** | Twitch ＆ YouTube Live 統合コメント＆アラートビュワー（自動翻訳・VOICEVOX 読み上げ・字幕連動） |
| **PngTuber** | OBS の音声レベルに連動する口パク・瞬き付き PNGTuber オーバーレイ |
| **Ticker** | 配信画面下部のスクロールティッカー |
| **Clock** | 日付・曜日・デジタル時計ウィジェット |
| **EndRoll** | チャット参加者を集計して流すエンドロール |
| **StampAlert_Twitch** | Twitch IRC 直接接続のスタンプカットインアラート |
| **KeyboardVisualizer** | MIDI 鍵盤ビジュアライザー（Web MIDI API） |
| **WebCanvas** | 画面注記ペン（Telestrator）と OBS 透過オーバーレイ |
| **jimakuChan-auto-lang** | 音声認識字幕ちゃんの翻訳言語自動切替 Chrome 拡張 |
| **twitch-audio-panner** | Chrome タブ別パンニング・EQ・LUFS メーター Chrome 拡張 |
| **server/** | 上記を配信するローカル Web サーバー（HTTP `8080` / TLS `8443`）と拡張機構 |

各ウィジェットの導入手順は、それぞれのフォルダ内の `README.md` が正本です。

## 使い方

### 配布パッケージを使う（推奨）

Releases から使いたいウィジェットの ZIP をダウンロードし、展開して OBS のブラウザソースに指定します。パッケージには必要なサーバー一式が同梱されています。

### リポジトリから動かす

```bash
git clone <this repository>
cd OBS_Widgets
python3 server.py
```

ブラウザソースの URL は `http://localhost:8080/<WidgetName>/` の形式です。

### 設定

- Twitch / YouTube 連携: `twitch_config.example.json` を `twitch_config.json` にコピーして編集します（ご自身の Twitch アプリの Client ID / Secret が必要です）。
- PngTuber: サーバー起動後にブラウザで設定ページ（`http://localhost:8080/PngTuber/settings.html`）を開いて、画像（4枚1組）・しきい値・対象マイクを設定できます（`pngtuber_config.json` に自動保存されます。手動編集する場合は `pngtuber_config.example.json` をコピーして使います。動作前提や詳細は [PngTuber/README.md](PngTuber/README.md) を参照）。
- OBS WebSocket のパスワード等は `shared/env-config.example.local.js` を `shared/env-config.local.js` にコピーして設定します。

設定ファイルの実物（トークン等を含むもの）は `.gitignore` で除外されており、リポジトリには含まれません。

## 設計

アーキテクチャとモジュール間契約は [SPEC.md](SPEC.md) にまとめています。自分の配信環境向けの追加ウィジェットやサーバーモジュールは、拡張ディレクトリ機構（`OBS_WIDGETS_EXTENSIONS_DIR`）で本体に手を入れずに足せます。

## 免責事項 (Disclaimer)

* 本プロジェクトは個人開発によるオープンソースの配信支援ツール群であり、OBS Project、Twitch Interactive, Inc.、Google LLC (YouTube)、および各関連企業の公式製品ではありません。
* YouTube Live チャット取得機能は非公式内部 API を利用した実験的機能です。仕様変更等により予告なく利用できなくなる場合があります。
* 本ソフトウェアの利用により発生したいかなるトラブルや損害に対しても、開発者は一切の責任を負いません。自己責任のもとでご利用ください。

## 関連ツール・謝辞

本プロジェクトは以下の素晴らしいソフトウェア・サービスの存在により成り立っています。開発者の皆様に深く感謝いたします。

* [OBS Studio / obs-websocket](https://obsproject.com/)
* [VOICEVOX](https://voicevox.hiroshiba.jp/) (AI 音声合成ソフトウェア)
* [わんコメ (OneComme)](https://onecomme.com/) (配信者向けコメントビューア)
* [音声認識字幕ちゃん](https://sayonari.github.io/jimakuChan/) (開発: さよなり @sayonari 氏)

## ライセンス

本ソフトウェアは [MIT License](LICENSE) のもとで公開されています。
