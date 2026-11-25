import json
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
