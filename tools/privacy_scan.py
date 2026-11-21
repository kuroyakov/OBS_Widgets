#!/usr/bin/env python3
"""個人情報スキャン。

配布物・コミットに個人情報が混ざっていないかを検査する。
CI (checks ワークフロー) とパッケージビルド (build_package.py) の両方から使う。

使い方:
    python3 tools/privacy_scan.py [--allowlist FILE] <path> [<path> ...]

- <path> はファイルまたはディレクトリ (再帰走査)。バイナリファイルは読み飛ばす。
- --allowlist は1行1パスの例外リスト (# 始まりはコメント)。
  CI でのみ使う既知例外用で、パッケージビルドでは適用しない。
- ヒットがあれば「ファイル:行:種別」を標準出力に出し、終了コード 1 を返す。
  ヒットした文字列そのものは出力しない (ログへの残留を避けるため)。

検出内容:
- 汎用パターン (このスクリプトに組み込み): LAN 内 IP アドレス、メールアドレス形式、
  ホームディレクトリの絶対パス (macOS / Windows / Linux のユーザーディレクトリ配下)。
- 個人リテラル (アカウント名・ホスト名・氏名など、リポジトリに含めてはいけない文字列):
  git 管理外の `tools/privacy_literals.local.json` から読み込む
  (形式は `tools/privacy_literals.example.json` を参照)。
  環境変数 `PRIVACY_SCAN_LITERALS_FILE` で読み込み先を変更できる。
  ファイルが無い場合は汎用パターンのみで検査する。
"""
import json
import os
import re
import sys
from pathlib import Path

# LAN 内の実アドレスのみ対象 (docstring 等の 192.168.x.x のような例示は対象外)
LAN_IP_RE = re.compile(r"(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}")

# メールアドレス形式。ただし Twitch IRC のホスト表記 (user@user.tmi.twitch.tv) は
# メールアドレスではないため対象外ドメインとする
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_EXCLUDED_SUFFIXES = (".tmi.twitch.tv",)
# icon@2x.png のようなアセットファイル名表記はメールアドレスではないため対象外
ASSET_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp", "svg", "ico",
                    "woff", "woff2", "ttf", "otf", "mp4", "webm", "avif")

# ホームディレクトリの絶対パス (OS 共通の汎用パターン)。
# スキャナ自身が検出対象の文字列リテラルを含まないよう、一部を分割表記にしている。
_HOME = "/" + "Users" + "/"
HOME_PATH_RES = (
    re.compile(re.escape(_HOME) + r"[^/\s\"']+/"),
    re.compile(r"[A-Za-z]:\\" + "Users" + r"\\[^\\\s\"']+\\"),
    re.compile(r"/home/[^/\s\"']+/"),
)

DEFAULT_LITERALS_FILE = Path(__file__).resolve().parent / "privacy_literals.local.json"


def load_literals():
    """個人リテラル (種別ラベル -> 文字列) を git 管理外ファイルから読み込む。"""
    path = Path(os.environ.get("PRIVACY_SCAN_LITERALS_FILE", DEFAULT_LITERALS_FILE))
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items() if v}


def scan_text(text, literals):
    """テキスト中のヒットを (行番号, 種別) のリストで返す。"""
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, needle in literals.items():
            if needle in line:
                hits.append((lineno, label))
        if LAN_IP_RE.search(line):
            hits.append((lineno, "lan-ip"))
        if any(rx.search(line) for rx in HOME_PATH_RES):
            hits.append((lineno, "home-path"))
        for m in EMAIL_RE.finditer(line):
            addr = m.group(0)
            domain = addr.split("@", 1)[1].lower()
            if any(domain.endswith(sfx) for sfx in EMAIL_EXCLUDED_SUFFIXES):
                continue
            if any(addr.lower().endswith("." + ext) for ext in ASSET_EXTENSIONS):
                continue
            hits.append((lineno, "email"))
    return hits


SELF = Path(__file__).resolve()


def iter_files(paths, extra_excludes=()):
    excludes = {SELF, *(Path(p).resolve() for p in extra_excludes)}
    for p in paths:
        p = Path(p)
        if p.is_dir():
            candidates = sorted(f for f in p.rglob("*") if f.is_file())
        elif p.is_file():
            candidates = [p]
        else:
            candidates = []
        for f in candidates:
            # スキャナ自身とリテラル定義ファイルはパターン文字列を含むため常に対象外
            if f.resolve() in excludes:
                continue
            yield f


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="個人情報スキャン")
    parser.add_argument("--allowlist", type=Path,
                        help="既知例外リスト (1行1パス、# はコメント)")
    parser.add_argument("paths", nargs="+", help="走査するファイルまたはディレクトリ")
    args = parser.parse_args(argv)

    allowlist = set()
    if args.allowlist:
        for line in args.allowlist.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                allowlist.add(os.path.normpath(line))

    literals = load_literals()
    literals_path = Path(os.environ.get("PRIVACY_SCAN_LITERALS_FILE", DEFAULT_LITERALS_FILE))

    total = 0
    for f in iter_files(args.paths, extra_excludes=(literals_path,)):
        # allowlist は repo 相対パスで照合する (./ 付きや .. を含む指定でも効くよう正規化)
        rel = os.path.normpath(str(f))
        if rel in allowlist:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue  # バイナリは対象外
        for lineno, label in scan_text(text, literals):
            print(f"{rel}:{lineno}: {label}")
            total += 1
    if total:
        print(f"privacy_scan: {total} hit(s)", file=sys.stderr)
        return 1
    print("privacy_scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
