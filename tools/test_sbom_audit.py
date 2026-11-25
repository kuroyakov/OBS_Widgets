import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import sbom_audit  # noqa: E402


class TestSbomAudit(unittest.TestCase):
    def test_repo_sbom_and_docs_are_in_sync(self):
        errors = sbom_audit.check_drift(
            sbom_audit.REPO_ROOT,
            sbom_audit.DEFAULT_SBOM_PATH,
            sbom_audit.DEFAULT_DOCS_PATH,
        )
        self.assertEqual(errors, [])

    def test_detects_new_external_domain_and_cdn_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)

            # 初期状態: 外部依存なし
            (root / "index.html").write_text("<html><body>hello</body></html>", encoding="utf-8")
            (root / "app.py").write_text("import json\n", encoding="utf-8")
            (root / "docs" / "DEPENDENCIES.md").write_text("`json`\n", encoding="utf-8")

            scan = sbom_audit.scan_repository(root)
            sbom_path = root / "sbom.cdx.json"
            sbom_path.write_text(
                json.dumps(sbom_audit.build_cyclonedx_sbom(scan), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                sbom_audit.check_drift(root, sbom_path, root / "docs" / "DEPENDENCIES.md"),
                [],
            )

            # 新しい外部 CDN と外部ドメインを追加したのに SBOM・台帳を更新しない場合
            (root / "index.html").write_text(
                '<script src="https://cdn.jsdelivr.net/npm/foo@1.2.3/dist/foo.js" '
                'integrity="sha384-abc"></script>\n'
                '<script>fetch("https://api.new-service.example.net/v1");</script>\n',
                encoding="utf-8",
            )
            errors = sbom_audit.check_drift(root, sbom_path, root / "docs" / "DEPENDENCIES.md")
            joined = "\n".join(errors)
            self.assertIn("SBOM drift detected", joined)
            self.assertIn("foo@1.2.3", joined)
            self.assertIn("api.new-service.example.net", joined)

    def test_wss_schemes_integrity_order_scoped_pkg_and_ci_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)

            # integrity が src より前 + scoped パッケージ + wss:// 専用ドメイン
            (root / "widget.html").write_text(
                '<script crossorigin="anonymous" integrity="sha384-scoped" '
                'src="https://cdn.jsdelivr.net/npm/@scope/pkg@2.0.0/dist/index.js"></script>\n'
                '<script>new WebSocket("wss://ws.stream.example.net/v1");</script>\n',
                encoding="utf-8",
            )
            # 同一行に複数 pip パッケージ + GitHub Action SHA 固定
            old_sha = "a" * 40
            new_sha = "b" * 40
            wf = root / ".github" / "workflows" / "ci.yml"
            wf.write_text(
                f"steps:\n"
                f"  - uses: actions/checkout@{old_sha} # v7.0.1\n"
                f"  - run: pip install -q alpha==1.0.0 beta==2.1.0\n",
                encoding="utf-8",
            )
            (root / "app.py").write_text("import certifi\n", encoding="utf-8")

            docs = root / "docs" / "DEPENDENCIES.md"
            docs.write_text(
                "@scope/pkg 2.0.0\n"
                "cdn.jsdelivr.net\n"
                "ws.stream.example.net\n"
                "certifi\n"
                "alpha==1.0.0\n"
                "beta==2.1.0\n"
                f"actions/checkout {old_sha} v7.0.1\n",
                encoding="utf-8",
            )

            scan = sbom_audit.scan_repository(root)
            self.assertEqual(scan["npm_cdn_libs"][0]["name"], "@scope/pkg")
            self.assertEqual(scan["npm_cdn_libs"][0]["integrity"], "sha384-scoped")
            self.assertEqual(
                [p["name"] for p in scan["ci_pip_packages"]],
                ["alpha", "beta"],
            )

            sbom = sbom_audit.build_cyclonedx_sbom(scan)
            ws_svc = next(s for s in sbom["services"] if s["name"] == "ws.stream.example.net")
            self.assertEqual(ws_svc["endpoints"], ["wss://ws.stream.example.net"])

            sbom_path = root / "sbom.cdx.json"
            sbom_path.write_text(json.dumps(sbom, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual(sbom_audit.check_drift(root, sbom_path, docs), [])

            # Action の SHA/バージョンだけ更新して docs/DEPENDENCIES.md が古いままの場合に検知すること
            wf.write_text(
                f"steps:\n"
                f"  - uses: actions/checkout@{new_sha} # v7.0.2\n"
                f"  - run: pip install -q alpha==1.0.0 beta==2.1.0\n",
                encoding="utf-8",
            )
            sbom_path.write_text(
                json.dumps(sbom_audit.build_cyclonedx_sbom(sbom_audit.scan_repository(root)), indent=2, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            errors = sbom_audit.check_drift(root, sbom_path, docs)
            self.assertTrue(any("actions/checkout" in e and new_sha in e for e in errors))

    def test_cli_main_generate_and_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            sbom_out = Path(tmp) / "sbom.cdx.json"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc_gen = sbom_audit.main(["--generate-sbom", "--sbom-path", str(sbom_out)])
                rc_chk = sbom_audit.main(
                    [
                        "--check",
                        "--sbom-path",
                        str(sbom_out),
                        "--docs-path",
                        str(sbom_audit.DEFAULT_DOCS_PATH),
                    ]
                )
            self.assertEqual(rc_gen, 0)
            self.assertEqual(rc_chk, 0)
            self.assertTrue(sbom_out.is_file())
            self.assertIn("OK:", buf.getvalue())

            with mock.patch("sys.exit", side_effect=SystemExit(2)):
                rc_help = sbom_audit.main([])
                self.assertEqual(rc_help, 2)


if __name__ == "__main__":
    unittest.main()
