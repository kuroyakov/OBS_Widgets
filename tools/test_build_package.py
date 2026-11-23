#!/usr/bin/env python3
"""build_package.py のパッケージ別リリースノート抽出ロジックの単体テスト (Issue #25)。

実行:
    python3 -m unittest tools/test_build_package.py
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_package  # noqa: E402


def _git(repo, *args):
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test-user",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test-user",
    })
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


class TestPackageScopedReleaseNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git(self.repo, "init")

        # 初期状態 (ticker-v0.1.0, twitch-audio-panner-v0.1.0)
        for rel in (
            "LICENSE",
            "Ticker/info.html",
            "Ticker/README.md",
            "PngTuber/index.html",
            "shared/api-token.js",
            "shared/env-config.js",
            "shared/env-config.example.local.js",
            "shared/obs-client.js",
            "shared/page-ready.js",
            "twitch-audio-panner/manifest.json",
            "twitch-audio-panner/generate_icons.py",
            ".github/workflows/checks.yml",
        ):
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"init {rel}\n", encoding="utf-8")

        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "chore: initial commit")
        _git(self.repo, "tag", "ticker-v0.1.0")
        _git(self.repo, "tag", "twitch-audio-panner-v0.1.0")

        # 無関係な変更 (PngTuber, checks.yml, LICENSE, exclude 対象)
        (self.repo / "PngTuber/index.html").write_text("pngtuber v2\n", encoding="utf-8")
        _git(self.repo, "add", "PngTuber/index.html")
        _git(self.repo, "commit", "-m", "feat(pngtuber): add mic filter")

        (self.repo / ".github/workflows/checks.yml").write_text("ci v2\n", encoding="utf-8")
        _git(self.repo, "add", ".github/workflows/checks.yml")
        _git(self.repo, "commit", "-m", "ci: run server unit tests")

        (self.repo / "LICENSE").write_text("license v2\n", encoding="utf-8")
        _git(self.repo, "add", "LICENSE")
        _git(self.repo, "commit", "-m", "docs: update LICENSE year")

        (self.repo / "twitch-audio-panner/generate_icons.py").write_text("gen v2\n", encoding="utf-8")
        _git(self.repo, "add", "twitch-audio-panner/generate_icons.py")
        _git(self.repo, "commit", "-m", "chore(panner): update excluded icon generator")

        # ticker 対象ファイルの変更 (shared/env-config.js)
        (self.repo / "shared/env-config.js").write_text("env v2\n", encoding="utf-8")
        _git(self.repo, "add", "shared/env-config.js")
        _git(self.repo, "commit", "-m", "fix(widgets): update shared env-config")
        _git(self.repo, "tag", "ticker-v0.1.1")
        _git(self.repo, "tag", "twitch-audio-panner-v0.1.1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ticker_release_notes_excludes_unrelated_commits(self):
        with mock.patch.object(build_package, "REPO", self.repo):
            notes = build_package.render_release_notes(
                "ticker",
                "0.1.1",
                repo="kuroyakov/OBS_Widgets",
                prev_tag="ticker-v0.1.0",
                target="ticker-v0.1.1",
            )
        self.assertIn("### 主な変更点（ticker-v0.1.0 → ticker-v0.1.1）", notes)
        self.assertIn("fix(widgets): update shared env-config", notes)
        self.assertIn(
            "**Full Changelog**: https://github.com/kuroyakov/OBS_Widgets/compare/ticker-v0.1.0...ticker-v0.1.1",
            notes,
        )
        self.assertNotIn("feat(pngtuber): add mic filter", notes)
        self.assertNotIn("ci: run server unit tests", notes)
        self.assertNotIn("docs: update LICENSE year", notes)

    def test_zero_matching_commits_uses_fallback_and_ignores_excluded_files(self):
        with mock.patch.object(build_package, "REPO", self.repo):
            notes = build_package.render_release_notes(
                "twitch-audio-panner",
                "0.1.1",
                repo="kuroyakov/OBS_Widgets",
                prev_tag="twitch-audio-panner-v0.1.0",
                target="twitch-audio-panner-v0.1.1",
            )
        self.assertIn("### 主な変更点（twitch-audio-panner-v0.1.0 → twitch-audio-panner-v0.1.1）", notes)
        self.assertIn("このパッケージに含まれるファイルへの直接の変更はありません", notes)
        self.assertNotIn("update excluded icon generator", notes)

    def test_without_prev_tag_omits_changelog_section(self):
        with mock.patch.object(build_package, "REPO", self.repo):
            notes = build_package.render_release_notes(
                "ticker",
                "0.1.0",
                repo="kuroyakov/OBS_Widgets",
                prev_tag=None,
            )
        self.assertNotIn("### 主な変更点", notes)
        self.assertNotIn("Full Changelog", notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
