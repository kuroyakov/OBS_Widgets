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
from privacy_scan import load_literals, scan_text  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXED_DATE = (2026, 1, 1, 0, 0, 0)

# パッケージ定義: 同梱ディレクトリ / shared の同梱ファイル / 追加パス
PACKAGES = {
    "clock": {
        "dirs": ["Clock"],
        "shared": [],
        "extra": [],
    },
    "endroll": {
        "dirs": ["EndRoll"],
        "shared": [],
        "extra": [],
    },
    "stampalert-twitch": {
        "dirs": ["StampAlert_Twitch"],
        "shared": ["env-config.js", "env-config.example.local.js"],
        "extra": [],
    },
    "twitch-audio-panner": {
        "dirs": ["twitch-audio-panner"],
        "shared": [],
        "extra": [],
        "exclude": ["twitch-audio-panner/generate_icons.py"],
    },
    "webcanvas": {
        "dirs": ["WebCanvas"],
        "shared": ["api-token.js", "env-config.js", "env-config.example.local.js",
                   "obs-client.js", "page-ready.js"],
        "extra": [],
        "exclude": ["WebCanvas/start.command"],
    },
    "keyboard": {
        "dirs": ["KeyboardVisualizer"],
        "shared": ["env-config.js", "env-config.example.local.js",
                   "obs-client.js", "page-ready.js"],
        "extra": [],
    },
    "ticker": {
        "dirs": ["Ticker"],
        "shared": ["api-token.js", "env-config.js", "env-config.example.local.js",
                   "obs-client.js", "page-ready.js"],
        "extra": [],
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
                  "server/translator.py", "server/twitch_api.py", "server/voicevox.py",
                  "server/youtube_chat.py", "twitch_config.example.json"],
    },
}


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
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
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
