# `twitch-audio-panner/` — MixConsole (ブラウザタブ別パンニング・EQ・聖徳太子モード拡張)

Chrome で開いている複数タブ（Twitch 複数視点配信・YouTube・BGM 等）の音声を個別にキャプチャし、**L/R パンニング・フェーダー音量・Mute/Solo・Arturia Pre 1973 風 3バンドEQ＋HPF・LUFSラウドネスメーター・ワンクリック「聖徳太子モード」（自動L/R振り分け＆音量均一化）** を提供する Chrome 拡張機能（Manifest V3）です。

## ファイル構成と主な責務

```text
twitch-audio-panner/
├── README.md                  # 本ドキュメント
├── manifest.json              # Chrome拡張 Manifest V3 定義
├── popup.html                 # ミキサーラック ＆ Pre 1973 EQモーダルのHTML構造 (~300行)
├── popup.css                  # ミキサーストリップ・アナログノブ・LEDメーター等のスタイル (~1,035行)
├── popup.js                   # チャンネルストリップ生成・聖徳太子オートパン/レベラー・EQモーダル連携
├── js/
│   └── rotary-and-utils.js    # 同軸ロータリーノブ操作・LUFS/RMS変換・メーター描画・フォーマット共通関数 (~255行)
├── content.js                 # 各タブ内に注入される Web Audio API グラフ (StereoPanner + 4段BiquadFilter + LUFS計測)
├── background.js              # タブ遷移・リロード時の設定自動再適用 Service Worker
└── icons/                     # 拡張機能アイコン ＆ 聖徳太子メダリオン画像アセット
```

## 各ファイルの役割

* **[popup.html](popup.html)** / **[popup.css](popup.css)**
  * ポップアップUIのマークアップとスタイルを分離。デザイン変更時は `popup.css` のみを編集します。
* **[js/rotary-and-utils.js](js/rotary-and-utils.js)**
  * ドラッグ・ダブルクリックリセット・ホイール微調整に対応した汎用ロータリーノブ制御 (`setupRotaryControl`)、LUFSメーター表示更新 (`updateTrackMeterUI`, `setLufsDisplay`)、周波数・ゲインの文字列フォーマット関数群。
* **[popup.js](popup.js)**
  * 音声再生中タブの検出、チャンネルストリップUI構築、Solo/Mute、聖徳太子モード（複数タブの自動L/R定位分散＋-15 LUFSターゲット自動ゲイン調整）、Pre 1973 EQモーダルのバインディング。
* **[content.js](content.js)**
  * ページ内の `<video>` / `<audio>` 要素を `AudioContext` に接続し、Input Trim → HPF → Low Shelf → Mid Peaking → High Shelf (12kHz) → StereoPanner → Master Gain → AnalyserNode の処理チェーンを構築。
* **[background.js](background.js)**
  * タブがリロードやURL遷移した際に `chrome.storage.local` から該当タブのパン・音量・EQ設定を自動復元し、タブが閉じられた時に設定をクリーンアップします。

## セットアップ（配布パッケージ利用時）

1. Chrome で `chrome://extensions` を開き、右上の「デベロッパーモード」を有効にします。
2. 「パッケージ化されていない拡張機能を読み込む」から、このフォルダを選択します。
3. 音声を再生しているタブを開いた状態で拡張機能のアイコンをクリックすると、タブごとのミキサーが表示されます。OBS は不要です。
