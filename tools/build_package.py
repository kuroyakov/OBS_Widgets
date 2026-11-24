#!/usr/bin/env python3
"""配布パッケージのビルド (Issue #146 / #147)。

使い方:
    python3 tools/build_package.py <package> <version> [--out DIR]
    python3 tools/build_package.py --list

設計:
- 同梱ファイルは **git 管理対象 (tracked) のみ**。git ls-files で列挙するため、
  個人設定 (shared/env-config.local.js、twitch_config.json) や第三者素材
  (KeyboardVisualizer/keyboard.png) のような git 管理外ファイルは構造的に入らない。
- ウィジェットが ../shared/ を相対参照する構造を保つため、パッケージ内にも
  shared/ を同階層に置き、必要なファイルだけを同梱する。
- ZIP 化の前にステージング全体へ個人情報スキャン (privacy_scan) を例外なしで
  適用し、ヒットしたら中止する。
- ZIP のメタデータは正規化する (全エントリの日時を 2026-01-01 00:00 に固定、
  並びはソート済み) ため、同じ内容からは同じ ZIP ができる。
"""
import argparse
import hashlib
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from privacy_scan import load_literals, read_text_for_scan, scan_text  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXED_DATE = (2026, 1, 1, 0, 0, 0)

# パッケージ定義: 同梱ディレクトリ / shared の同梱ファイル / 追加パス / リリースノート用メタ情報
PACKAGES = {
    "clock": {
        "dirs": ["Clock"],
        "shared": [],
        "extra": [],
        "title": "Clock（日付・デジタル時計ウィジェット）",
        "summary": "OBS の配信画面隅に配置する、日付・曜日・現在時刻（秒付き）のデジタル時計オーバーレイです。",
        "runtime": "Python サーバー不要（OBS のブラウザソースでローカルファイル `Clock/Date.html` を指定）",
        "readme": "Clock/README.md",
    },
    "endroll": {
        "dirs": ["EndRoll"],
        "shared": [],
        "extra": [],
        "title": "EndRoll（わんコメ連携エンドロール）",
        "summary": "わんコメ (OneComme) が収集したチャット・通知から配信参加者を自動集計し、映画風のエンドロールを流すウィジェットです。",
        "runtime": "Python サーバー不要（わんコメのテンプレートフォルダへ配置して使用）",
        "readme": "EndRoll/README.md",
    },
    "stampalert-twitch": {
        "dirs": ["StampAlert_Twitch"],
        "shared": ["env-config.js", "env-config.example.local.js"],
        "extra": [],
        "title": "StampAlert_Twitch（Twitch スタンプカットインアラート）",
        "summary": "Twitch IRC チャットに直接接続し、スタンプ付きコメントを画面左端からカットイン表示するオーバーレイウィジェットです。",
        "runtime": "Python サーバー不要（OBS のブラウザソースで `StampAlert_Twitch/stamp-alert.html` を指定）",
        "readme": "StampAlert_Twitch/README.md",
    },
    "twitch-audio-panner": {
        "dirs": ["twitch-audio-panner"],
        "shared": [],
        "extra": [],
        "exclude": ["twitch-audio-panner/generate_icons.py"],
        "title": "twitch-audio-panner（Chrome タブ別ミキサー＆パンニング拡張）",
        "summary": "Chrome タブ別の L/R パンニング・3バンド EQ・LUFS メーター・オートパン＆オートレベラー（聖徳太子モード）を提供する Chrome 拡張機能です。",
        "runtime": "Chrome 拡張（ZIP を展開し、`chrome://extensions` の「パッケージ化されていない拡張機能を読み込む」から `twitch-audio-panner/` を選択）",
        "readme": "twitch-audio-panner/README.md",
    },
    # WebCanvas セット: iPad 等の別端末から pad.html を開くために統合サーバーを同梱する
    # (HTTPS ポート 8443 および /obs-ws トンネルを提供)。
    "webcanvas": {
        "dirs": ["WebCanvas"],
        "shared": ["api-token.js", "env-config.js", "env-config.example.local.js",
                   "obs-client.js", "page-ready.js"],
        "extra": ["server.py",
                  "server/__init__.py", "server/config.py", "server/event_log.py",
                  "server/extensions.py", "server/feed_log.py", "server/handler.py",
                  "server/translator.py", "server/twitch_api.py", "server/voicevox.py",
                  "server/youtube_chat.py"],
        "title": "WebCanvas（画面注記ペン / Telestrator）",
        "summary": "タブレットやブラウザから OBS 映像ソースへペン・矢印・図形を描き込み、OBS 透過オーバーレイへリアルタイム反映する注記ツールです。",
        "runtime": "Python 3（iPad 等から描く場合は同梱の `python3 server.py` 起動が必要、同一 PC 内のみならローカルファイル動作も可）＋ OBS Studio 28+（obs-websocket 有効）",
        "readme": "WebCanvas/README.md",
    },
    "keyboard": {
        "dirs": ["KeyboardVisualizer"],
        "shared": ["api-token.js", "env-config.js", "env-config.example.local.js",
                   "obs-client.js", "page-ready.js"],
        "extra": [],
        "title": "KeyboardVisualizer（MIDI 鍵盤ビジュアライザー）",
        "summary": "Web MIDI API で受け取った演奏入力を OBS 透過オーバーレイ上の鍵盤にリアルタイム表示するビジュアライザーです。",
        "runtime": "Python サーバー不要・OBS Studio 28+（obs-websocket 有効）および Web MIDI 対応ブラウザ（Chrome 等）が必要",
        "readme": "KeyboardVisualizer/README.md",
    },
    "ticker": {
        "dirs": ["Ticker"],
        "shared": ["api-token.js", "env-config.js", "env-config.example.local.js",
                   "obs-client.js", "page-ready.js"],
        "extra": [],
        "title": "Ticker（配信情報スクロールティッカー）",
        "summary": "OBS 配信画面の上部・下部にお知らせテキストをシームレスにスクロール表示するティッカーウィジェットです。",
        "runtime": "Python サーバー不要（OBS ブラウザソースで `Ticker/info.html` を指定、他画面連携時は obs-websocket 使用）",
        "readme": "Ticker/README.md",
    },
    # PngTuber セット: 統合サーバーと OBS 音声計測モジュールを同梱する。
    # handler.py がトップレベルで import する基本モジュール群に加え、
    # obs_ws / pngtuber_audio と設定テンプレートを含める。
    "pngtuber": {
        "dirs": ["PngTuber"],
        "shared": ["api-token.js", "env-config.js", "env-config.example.local.js",
                   "page-ready.js"],
        "extra": ["server.py",
                  "server/__init__.py", "server/config.py", "server/event_log.py",
                  "server/extensions.py", "server/feed_log.py", "server/handler.py",
                  "server/obs_ws.py", "server/pngtuber_audio.py",
                  "server/translator.py", "server/twitch_api.py", "server/voicevox.py",
                  "server/youtube_chat.py", "pngtuber_config.example.json"],
        "title": "PngTuber（OBS 音声連動 PNGTuber オーバーレイ）",
        "summary": "OBS のマイク音量に連動した口パク・瞬きおよび複数アバター設定 UI に対応した PNGTuber オーバーレイウィジェットです。",
        "runtime": "Python 3（同梱の `python3 server.py` 起動）＋ OBS Studio 28+（obs-websocket 有効）が必要",
        "readme": "PngTuber/README.md",
    },
    # CommentViewer セット: サーバーを同梱ランタイムとして含む。
    # 同梱サーバーは CommentViewer が使うモジュールだけに限定する (Issue #150 PR-3):
    # Controller 系 (launcher / browser_refresh / display_recapture /
    # controller_status) と、その専用依存 obs_ws は含めない。handler は
    # それらの不在を検出して関連 API を無効化する。
    # 起動用の .command や obs_mothership.lua は配布に含めない (決定事項)。
    "commentviewer": {
        "dirs": ["CommentViewer", "jimakuChan-auto-lang"],
        "shared": "ALL",
        "extra": ["server.py",
                  "server/__init__.py", "server/config.py", "server/event_log.py",
                  "server/extensions.py", "server/feed_log.py", "server/handler.py",
                  "server/obs_ws.py", "server/translator.py", "server/twitch_api.py",
                  "server/voice_command.py", "server/voicevox.py",
                  "server/youtube_chat.py", "twitch_config.example.json",
                  "voice_command_config.example.json"],
        "title": "CommentViewer（Twitch ＆ YouTube Live 統合コメント＆アラートビュワー）",
        "summary": "Twitch と YouTube Live のチャット・アラートを統合表示し、自動翻訳・VOICEVOX 読み上げ・音声認識字幕ちゃん連動（`jimakuChan-auto-lang` 同梱）を行うウィジェットセットです。",
        "runtime": "Python 3（同梱の `python3 server.py` 起動）が必要",
        "readme": "CommentViewer/README.md",
    },
    "voicecommand": {
        "dirs": ["VoiceCommand", "jimakuChan-auto-lang"],
        "shared": ["api-token.js", "env-config.js", "env-config.example.local.js"],
        "extra": ["server.py",
                  "server/__init__.py", "server/config.py", "server/event_log.py",
                  "server/extensions.py", "server/feed_log.py", "server/handler.py",
                  "server/obs_ws.py", "server/subtitle_mute_link.py",
                  "server/translator.py", "server/twitch_api.py",
                  "server/voice_command.py", "server/voicevox.py",
                  "server/youtube_chat.py", "voice_command_config.example.json"],
        "title": "VoiceCommand（音声認識字幕ちゃん連動 OBS 音声コマンド）",
        "summary": "音声認識字幕ちゃんの確定発話で OBS の音量調整・ミュート・フィルタ切替・ホットキー実行・離席中モード切替を行う設定 UI とバックエンドです。",
        "runtime": "Python 3（同梱の `python3 server.py` 起動）＋ OBS Studio 28+（obs-websocket 有効）＋ Chrome 拡張 `jimakuChan-auto-lang` が必要",
        "readme": "VoiceCommand/README.md",
    },
}


def collect_package_commits(pkg, prev_tag, target="HEAD"):
    """prev_tag..target 間でパッケージ同梱対象ファイルに触れた非マージコミット一覧を返す。"""
    paths = [rel for rel, _ in collect(pkg) if rel != "LICENSE"]
    if not paths:
        return []
    out = subprocess.run(
        ["git", "log", "--no-merges", "--format=%H\t%s", f"{prev_tag}..{target}", "--", *paths],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    commits = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition("\t")
        commits.append((sha, subject))
    return commits


def render_release_notes(pkg, version, repo=None, prev_tag=None, target="HEAD"):
    """GitHub Release 用の本文 (Markdown) を生成する。"""
    spec = PACKAGES[pkg]
    tag = f"{pkg}-v{version}"
    zip_name = f"{tag}.zip"
    readme = spec["readme"]
    if repo:
        readme_ref = f"[{readme}](https://github.com/{repo}/blob/{tag}/{readme})"
    else:
        readme_ref = f"`{readme}`"
    lines = [
        f"## {spec['title']} (`v{version}`)",
        "",
        spec["summary"],
        "",
        "### 動作前提",
        f"- {spec['runtime']}",
        "",
        "### 導入方法",
        f"1. 下部のアセットから `{zip_name}` をダウンロードして展開してください。",
        f"2. 詳細なセットアップ手順は同梱の {readme_ref} を参照してください。",
        "3. ダウンロードファイルの整合性確認には `SHA256SUMS.txt` を利用できます。",
        "",
    ]
    if prev_tag:
        commits = collect_package_commits(pkg, prev_tag, target=target)
        lines.append(f"### 主な変更点（{prev_tag} → {tag}）")
        if commits:
            for sha, subject in commits:
                short_sha = sha[:7]
                if repo:
                    lines.append(f"- {subject} ([{short_sha}](https://github.com/{repo}/commit/{sha}))")
                else:
                    lines.append(f"- {subject} ({short_sha})")
        else:
            lines.append("- このパッケージに含まれるファイルへの直接の変更はありません（共通設定・リリース基盤等の更新による再ビルド）。")
        if repo:
            lines.append("")
            lines.append(f"**Full Changelog**: https://github.com/{repo}/compare/{prev_tag}...{tag}")
        lines.append("")
    return "\n".join(lines)


def tracked_files(paths):
    """指定パスの git 管理対象ファイルを repo 相対で返す。

    -z を使うため、非 ASCII や特殊文字を含むパスでも壊れない。
    paths が空のとき git ls-files は全ファイルを返してしまうので、ここで止める。
    """
    if not paths:
        return []
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *paths],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    return [p for p in out.stdout.split("\0") if p]


def collect(pkg):
    """パッケージに同梱する (repo相対パス, ZIP内パス) の一覧を返す。"""
    spec = PACKAGES[pkg]
    items = []
    # dirs / extra とも fail-closed: 指定パス (ディレクトリ・個別ファイル) の
    # 管理対象が1件も無ければ、定義の誤記や git add 忘れとしてエラーにする
    # (黙って不完全なパッケージを作らない)。
    for rel in spec["dirs"] + spec["extra"]:
        got = tracked_files([rel])
        if not got:
            raise ValueError(f"package path not tracked or missing: {rel}")
        items.extend((f, f) for f in got)
    if spec["shared"] == "ALL":
        shared = tracked_files(["shared"])
    else:
        # 個別指定も必ず git 管理対象チェックを通す。定義の誤記で
        # ファイルが欠けた場合は、黙って不完全なパッケージを作らずエラーにする。
        wanted = [f"shared/{name}" for name in spec["shared"]]
        shared = tracked_files(wanted)
        missing = [w for w in wanted if w not in shared]
        if missing:
            raise ValueError(f"shared files not tracked or missing: {missing}")
    for rel in shared:
        items.append((rel, rel))
    if not tracked_files(["LICENSE"]):
        raise ValueError("LICENSE is not tracked")
    items.append(("LICENSE", "LICENSE"))
    # 除外指定の適用 (定義の誤記を検知するため、一致しない除外パスはエラーにする)
    excludes = set(spec.get("exclude", ()))
    if excludes:
        present = {arc for _, arc in items}
        unmatched = sorted(excludes - present)
        if unmatched:
            raise ValueError(f"exclude paths not found in package: {unmatched}")
        items = [(rel, arc) for rel, arc in items if arc not in excludes]
    # 重複除去 (順序保持)
    seen = set()
    unique = []
    for rel, arc in items:
        if arc not in seen:
            seen.add(arc)
            unique.append((rel, arc))
    return unique


def main(argv=None):
    parser = argparse.ArgumentParser(description="配布パッケージのビルド")
    parser.add_argument("package", nargs="?", help="パッケージ名 (--list 参照)")
    parser.add_argument("version", nargs="?", help="バージョン X.Y.Z")
    parser.add_argument("--out", type=Path, default=Path("dist"), help="出力先")
    parser.add_argument("--list", action="store_true", help="パッケージ名の一覧")
    parser.add_argument("--release-notes", action="store_true",
                        help="GitHub Release 用のリリースノートを標準出力へ出力して終了")
    parser.add_argument("--repo", help="GitHub リポジトリ (owner/repo、README リンク生成用)")
    parser.add_argument("--prev-tag", default=None, help="差分抽出の起点タグ (省略時は変更点セクションを出さない)")
    parser.add_argument("--target", default="HEAD", help="差分抽出の終点 git ref (既定: HEAD)")
    args = parser.parse_args(argv)
    if args.list:
        for name in PACKAGES:
            print(name)
        return 0
    if not args.package or not args.version:
        parser.print_usage(sys.stderr)
        return 2
    pkg, version, out_dir = args.package, args.version, args.out
    if pkg not in PACKAGES:
        print(f"unknown package: {pkg} (see --list)", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"version must be X.Y.Z: {version}", file=sys.stderr)
        return 2
    if args.release_notes:
        print(
            render_release_notes(
                pkg,
                version,
                repo=args.repo,
                prev_tag=args.prev_tag or None,
                target=args.target,
            ),
            end="",
        )
        return 0

    root_name = f"{pkg}-v{version}"
    try:
        items = collect(pkg)
    except ValueError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 2
    if not items:
        print("no files collected", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / root_name
        for rel, arc in items:
            dest = stage / arc
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((REPO / rel).read_bytes())

        # 個人情報スキャン (例外なし)。ヒットしたら ZIP 化しない。
        hits = []
        for f in sorted(stage.rglob("*")):
            if not f.is_file():
                continue
            text = read_text_for_scan(f)
            if text is None:
                continue
            for lineno, label in scan_text(text, load_literals()):
                hits.append(f"{f.relative_to(stage)}:{lineno}: {label}")
        if hits:
            print("privacy scan FAILED; package not built:", file=sys.stderr)
            for h in hits:
                print(f"  {h}", file=sys.stderr)
            return 1

        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / f"{root_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(stage.rglob("*")):
                if not f.is_file():
                    continue
                arc = f.relative_to(Path(tmp)).as_posix()
                info = zipfile.ZipInfo(arc, date_time=FIXED_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if f.suffix == ".command" else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                zf.writestr(info, f.read_bytes())

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    print(f"built: {zip_path} ({zip_path.stat().st_size} bytes, {len(items)} files)")
    print(f"sha256: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
