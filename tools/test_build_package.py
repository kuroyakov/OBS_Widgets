#!/usr/bin/env python3
"""配布パッケージ構成・参照整合性・README・release.yml の検査テスト (Issue #33)。

実行: python3 -m unittest discover -s tools -p 'test_*.py'
"""

from html.parser import HTMLParser
from pathlib import Path
import posixpath
import re
import sys
import unittest

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_package  # noqa: E402


ALLOWED_EXTERNAL_OR_USER_REFS = {
    "shared/env-config.local.js",
    "KeyboardVisualizer/keyboard.png",
}


class _AssetRefParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if tag == "script" and "src" in attr_map:
            self.refs.append(attr_map["src"])
        elif tag == "link" and "href" in attr_map:
            rel = str(attr_map.get("rel") or "").lower()
            if rel in ("", "stylesheet", "icon", "shortcut icon", "apple-touch-icon"):
                self.refs.append(attr_map["href"])


def _is_ignored_url(ref):
    s = str(ref or "").strip()
    if not s or s.startswith(("#", "data:", "http://", "https://", "//", "blob:")):
        return True
    return False


def _resolve_zip_ref(html_zip_path, ref):
    clean = str(ref).split("?", 1)[0].split("#", 1)[0].strip()
    if clean.startswith("/"):
        return clean.lstrip("/")
    base_dir = posixpath.dirname(html_zip_path)
    joined = posixpath.join(base_dir, clean) if base_dir else clean
    return posixpath.normpath(joined)


def _extract_release_yml_package_options(yml_text):
    options = []
    in_options = False
    options_indent = None
    for line in yml_text.splitlines():
        if not in_options:
            if re.match(r"^\s*options:\s*$", line):
                in_options = True
                options_indent = len(line) - len(line.lstrip())
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= options_indent:
            break
        m = re.match(r"^\s*-\s*([A-Za-z0-9_-]+)\s*$", line)
        if m:
            options.append(m.group(1))
    return options


class TestBuildPackageIntegrity(unittest.TestCase):
    def test_collect_succeeds_for_all_packages(self):
        for pkg in build_package.PACKAGES:
            items = build_package.collect(pkg)
            self.assertGreater(len(items), 0, f"{pkg}: collect() returned empty")

    def test_html_local_asset_references_exist_in_package(self):
        for pkg in build_package.PACKAGES:
            items = build_package.collect(pkg)
            zip_paths = {zip_rel for _, zip_rel in items}
            for repo_rel, zip_rel in items:
                if not zip_rel.lower().endswith((".html", ".htm")):
                    continue
                html_text = (REPO_ROOT / repo_rel).read_text(encoding="utf-8")
                parser = _AssetRefParser()
                parser.feed(html_text)
                html_dir = posixpath.dirname(zip_rel)
                for raw_ref in parser.refs:
                    if _is_ignored_url(raw_ref):
                        continue
                    clean_ref = raw_ref.split("?", 1)[0].split("#", 1)[0].strip()
                    # わんコメ側で実行時に提供される onesdk.js は例外許可
                    if posixpath.basename(clean_ref) == "onesdk.js":
                        continue
                    resolved = _resolve_zip_ref(zip_rel, clean_ref)
                    if resolved in ALLOWED_EXTERNAL_OR_USER_REFS:
                        continue
                    # ルート相対でない相対パスがパッケージ外 (..) に出る場合は onesdk.js 以外エラー
                    self.assertFalse(
                        resolved.startswith(".."),
                        f"{pkg}: {zip_rel} references outside package: {raw_ref} (from {html_dir})",
                    )
                    self.assertIn(
                        resolved,
                        zip_paths,
                        f"{pkg}: {zip_rel} references missing asset '{raw_ref}' (resolved: '{resolved}')",
                    )

    def test_all_package_readmes_have_setup_section(self):
        for pkg, spec in build_package.PACKAGES.items():
            readme_rel = spec["readme"]
            readme_path = REPO_ROOT / readme_rel
            self.assertTrue(readme_path.is_file(), f"{pkg}: missing README {readme_rel}")
            text = readme_path.read_text(encoding="utf-8")
            self.assertIn(
                "## セットアップ（配布パッケージ利用時）",
                text,
                f"{pkg}: {readme_rel} missing '## セットアップ（配布パッケージ利用時）' section",
            )

    def test_release_yml_options_match_packages_keys(self):
        release_yml = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        yml_options = _extract_release_yml_package_options(release_yml)
        self.assertEqual(
            set(yml_options),
            set(build_package.PACKAGES.keys()),
            f"release.yml options {yml_options} do not match PACKAGES keys {list(build_package.PACKAGES.keys())}",
        )
        self.assertEqual(len(yml_options), len(set(yml_options)), "release.yml has duplicate package options")


if __name__ == "__main__":
    unittest.main(verbosity=2)
