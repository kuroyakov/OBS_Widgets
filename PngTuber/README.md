# `PngTuber/` — PNGTuber オーバーレイウィジェット

OBS ブラウザソース上でアバターを直接描画する透過オーバーレイウィジェットです。マイク音量に連動した口パク・瞬きに対応し、複数アバター（体ごとに画像セットとマイクを指定可能）にも対応します。

> **動作前提**: 本ウィジェットは単体アプリではありません。**OBS Studio 28+（obs-websocket 有効）** と **同梱の統合サーバー（`python3 server.py`）の起動** が前提です。

## ファイル構成と主な責務

```text
PngTuber/
├── README.md          # 本ドキュメント
├── index.html         # OBS ブラウザソース用エントリ（4枚レイヤー + ?debug=1 表示）
├── settings.html      # 通常ブラウザ用設定ページ（4枚画像・しきい値・対象マイク設定）
├── css/
│   ├── style.css      # 透過背景・無ちらつきレイヤー切替・デバッグオーバーレイ装飾
│   └── settings.css   # 設定ページ用スタイル
└── js/
    ├── app.js         # 画像プリロード・SSEレベル受信・音節切れ目(dip)対応口パク・瞬き・BroadcastChannel 受け口
    └── settings.js    # 設定ページ制御（画像アップロード/選択・ライブメーター付きしきい値調整・OBS マイク選択）
```

## 設定画面（`settings.html`）

統合サーバー起動後、通常のブラウザで `http://localhost:8080/PngTuber/settings.html` を開くと、アバターごとの設定を GUI から行えます。画面上部の「対象アバター」でメインアバターと `avatars` の各アバターを切り替えられ、新規アバターの追加もできます。

- **対象マイクソース**: OBS に接続できる場合は音声入力一覧からドロップダウンで選択（空＝自動選択）、OBS 未接続時はソース名の直接入力にフォールバックします。
- **入力レベルとしきい値**: SSE ライブメーター上に `open_threshold` / `close_threshold` のラインを表示しながらスライダーで調整できます。
- **アバター画像（4枚1組）**: `.png` ファイルをアップロードして `PngTuber/images/` へ保存＋プレビュー確認できるほか、`PngTuber/images/` に既存の画像から選ぶこともできます。

保存内容は `pngtuber_config.json` へ既存キー（`avatars` 等）を壊さずにマージ書き出しされ、サーバーが自動検出して反映するため再起動は不要です。

## 設定（`pngtuber_config.json`）

設定画面を使わず手動編集する場合は、リポジトリルートの [pngtuber_config.example.json](../pngtuber_config.example.json) を `pngtuber_config.json` にコピーして使用します（`pngtuber_config.json` と `PngTuber/images/` は `.gitignore` 対象）。未作成時は `pngtuber_config.example.json`＋既定値で動作しますが、初期状態では `PngTuber/images/` にサンプル画像は含まれないため、初回は設定画面から `.png` をアップロードするか手動で画像を配置してください。

```json
{
  "images": {
    "mouth_open_eye_open": "images/mouth_open_eye_open.png",
    "mouth_open_eye_closed": "images/mouth_open_eye_closed.png",
    "mouth_closed_eye_open": "images/mouth_closed_eye_open.png",
    "mouth_closed_eye_closed": "images/mouth_closed_eye_closed.png"
  },
  "obs_input_name": "",
  "open_threshold": 0.05,
  "close_threshold": 0.025,
  "dip_close_ratio": 0.82,
  "blink_min_ms": 2200,
  "blink_max_ms": 5500,
  "poll_ms": 120
}
```

| 設定項目 | 説明 | 既定値 |
|---|---|---|
| `images.mouth_open_eye_open` | 口開・目開の画像パスまたは URL | `images/mouth_open_eye_open.png` |
| `images.mouth_open_eye_closed` | 口開・目閉（瞬き）の画像パスまたは URL | `images/mouth_open_eye_closed.png` |
| `images.mouth_closed_eye_open` | 口閉・目開の画像パスまたは URL | `images/mouth_closed_eye_open.png` |
| `images.mouth_closed_eye_closed` | 口閉・目閉（瞬き）の画像パスまたは URL | `images/mouth_closed_eye_closed.png` |
| `obs_input_name` | 計測対象の OBS 音声ソース名（完全一致優先・大文字小文字無視の部分一致、`""` で OBS のマイク入力ソースを自動選択。旧キー `mic_device_name` もフォールバック受理） | `""` |
| `obs_ws_host`（任意） | OBS WebSocket ホストの上書き指定（未設定時は `twitch_config.json` または `127.0.0.1`） | `""` |
| `obs_ws_port`（任意） | OBS WebSocket ポートの上書き指定（未設定時は `twitch_config.json` の `obs_ws_port` または `4455`） | `null` |
| `obs_ws_password`（任意） | OBS WebSocket パスワードの上書き指定（未設定時は `twitch_config.json` の `obs_password` → `shared/env-config.local.js` の `OBS_DEFAULT_PASSWORD` を流用） | `""` |
| `open_threshold` | 口を開く正規化音量しきい値（`0.0` 〜 `1.0`、`0.05 ≈ -26 dBFS`） | `0.05` |
| `close_threshold` | 口を閉じる絶対正規化音量しきい値（ヒステリシス用、`open_threshold` 以下、`0.025 ≈ -32 dBFS`） | `0.025` |
| `dip_close_ratio`（任意） | 発話中に直近開口ピークに対する音量比がこの値以下へ減衰（音節の切れ目・子音落ち）した際に一瞬口を閉じる感度（`0.0`〜`0.98`、`0` で従来の絶対しきい値のみ動作）。コンプレッサー適用マイク等で開きっぱなしになる場合は `0.85` 前後へ上げると切れやすくなります | `0.82` |
| `min_open_ms`（任意） | 一度口を開いてから閉じるまでの最小維持時間（ms、`0`〜`1000`） | `80` |
| `min_closed_ms`（任意） | 音節切れ目（`dip`）で一瞬閉じてから再度開くまでの最小維持時間（ms、`0`〜`1000`。無音からの話し始めには適用せず即開口） | `45` |
| `blink_min_ms` | 瞬き間隔の下限（ms） | `2200` |
| `blink_max_ms` | 瞬き間隔の上限（ms） | `5500` |
| `poll_ms` | SSE 非対応環境・エラー時フォールバック用の `GET /api/pngtuber/level` ポーリング間隔（ms、既定経路は SSE 即時プッシュ） | `120` |

* 画像パスは `PngTuber/` 相対パス（例: `images/...`）、`http(s)://` URL、およびローカルファイルパス（`~/...` や絶対パス）のいずれも指定できます。ローカル絶対パスはサーバー側の `GET /api/pngtuber/image/<slot>` 経由で配信されるため、設定ファイル上のフルパスをブラウザや LAN 側へ露出しません。

## 音声レベル計測・低遅延配信方式と依存関係

バックエンドは [server/pngtuber_audio.py](../server/pngtuber_audio.py) と [server/obs_ws.py](../server/obs_ws.py) が担います。

* **OBS WebSocket 経由計測（外部依存ゼロ）**:
  * **OBS Studio 28+ 標準の obs-websocket (v5)** に接続し、`InputVolumeMeters` イベント（`EventSubscription::InputVolumeMeters`）と入力系イベントを購読して対象ソースのリアルタイム音量（`magnitude`、`0.0`〜`1.0`）を取得します。
  * **`ffmpeg` や `sounddevice` などの外部 CLI / Python ライブラリは一切不要**で、Python 標準ライブラリのみで動作します。
  * OBS 音声ミキサー側で適用したフィルタ（ノイズ抑制・コンプレッサー・ゲイン・ミュート）通過後の音量と完全一致するため、キーボード音や環境音で意図せず口パクしにくくなります。
* **低遅延プッシュ（SSE）と OBS 側の上限**:
  * 通常時は `GET /api/pngtuber/stream`（Server-Sent Events / `EventSource`）で常時接続し、OBS から `InputVolumeMeters` を受け取った瞬間に二重スムージング（EMA 減衰）なしでフロントエンドへ即時プッシュします。
  * なお、OBS Studio（`obs-websocket` v5）の `InputVolumeMeters` イベント自体が **約 50ms 間隔（20 Hz）固定配信**という OBS 側の仕様上限を持つため、サーバー〜ウィジェット間の転送遅延を 0ms に近づけた状態でも、音声入力から口開閉までの最小粒度は 50ms（0〜50ms + 数ms）が物理的な上限になります。
* **動作特性**:
  * 音声データの録音・保存は一切行わず、OBS WebSocket から届く音量レベル値のみを参照します。
  * OBS 未起動・WebSocket 未接続・認証不一致・対象ソース不在時は例外で落とさず `device_connected: false`・`level: 0.0` を返し、2 秒間隔で再接続を自動試行します。
  * OBS 上で対象マイクがミュートされた場合（`InputMuteStateChanged` / `GetInputMute`）は即座に `level: 0.0`・`speaking: false` として口を閉じます。
  * すべての SSE ストリームが切断され、かつポーリングアクセスも 15 秒間途絶えると WebSocket 接続を閉じて計測を一時停止し、OBS 側での高頻度メーター送信負荷をなくします（再アクセス時に自動再接続）。

## OBS ブラウザソース設定とデバッグ表示

* **通常 URL（配信用）**: `http://localhost:8080/PngTuber/index.html`
  * 背景は完全透過で、UI 要素は一切表示されません。
* **デバッグ表示 URL（実機調整用）**: `http://localhost:8080/PngTuber/index.html?debug=1`
  * `?debug=1` を付けた場合のみ、左上にレベル値（表示値・生値）、しきい値マーカー付きメーター、現在の口/目状態・画像スロット名、OBS マイク接続状態＋転送方式 (`[sse]` / `[poll]`)、受信した state 名を小さくオーバーレイ表示します。

## 将来の `state` 切替に向けた契約（予約済み）

外部の操作ページから将来表情・ポーズを切り替えるため、同一オリジンの `BroadcastChannel` 受け口をあらかじめ用意しています（Phase 1 では画像差し替えは行わず、受信した `name` の記録と `?debug=1` 表示のみを行います）。

* **チャネル名**: `pngtuber`
* **メッセージ形式**:
  ```json
  { "type": "state", "name": "happy" }
  ```
* **`name` の命名規則**:
  * 小文字英数字・ハイフン・アンダースコア（`^[a-z0-9_-]+$`）を推奨します。
  * 基本表情・通常状態は `"default"` を予約名とします（例: `"default"`, `"happy"`, `"angry"`, `"surprised"`, `"sleepy"`）。

## 関連 API

### 読み取り（GET）

* `GET /api/pngtuber/stream`: Server-Sent Events による音量レベル・発話判定・接続状態のリアルタイムプッシュ配信（`data: {"level": 0.42, "speaking": true, "device_connected": true}\n\n`、2 秒ごと `: keepalive`）
* `GET /api/pngtuber/level`: 単発 JSON 取得（`{"level": 0.42, "speaking": true, "device_connected": true}`、SSE フォールバック・外部確認用）
* `GET /api/pngtuber/config`: 解決済み画像 URL・画像ラベル・対象ソース設定・しきい値/`dip_close_ratio`・瞬き/開閉ホールド設定
* `GET /api/pngtuber/image/<slot>`: 設定されたローカル画像ファイル（4スロットのみ許可）の配信
* `GET /api/pngtuber/inputs`: OBS の音声入力ソース名一覧（未接続時は `connected: false` と空配列を返却）
* `GET /api/pngtuber/images`: `PngTuber/images/` 配下に存在する既存画像ファイルの相対パス一覧

### 設定保存・画像アップロード（POST・localhost または API トークン必須）

* `POST /api/pngtuber/config`: 設定を `pngtuber_config.json` へ原子的にマージ保存（対象外のアバターを含む未知のキーは保持）。ボディに `avatar` を付けるとそのアバターが保存対象になる
* `POST /api/pngtuber/image/<slot>`: `.png` 画像をサニタイズして `PngTuber/images/` へ保存し、`pngtuber_config.json` の該当スロットを更新（`?avatar=` で対象アバターを指定可能）
