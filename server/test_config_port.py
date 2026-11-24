import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from server.config import (
    DEFAULT_PORT,
    DEFAULT_TLS_PORT,
    ENV_PORT,
    ENV_TLS_PORT,
    resolve_port,
    resolve_ports,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestResolvePort(unittest.TestCase):
    def test_defaults_when_unset(self):
        self.assertEqual(resolve_port(ENV_PORT, DEFAULT_PORT, environ={}), 8080)
        self.assertEqual(resolve_port(ENV_TLS_PORT, DEFAULT_TLS_PORT, environ={}), 8443)
        self.assertEqual(resolve_ports(environ={}, tls_enabled=False), (8080, 8443))

    def test_defaults_when_empty_or_whitespace(self):
        env = {ENV_PORT: "", ENV_TLS_PORT: "   "}
        self.assertEqual(resolve_ports(environ=env, tls_enabled=False), (8080, 8443))

    def test_valid_custom_ports(self):
        self.assertEqual(
            resolve_ports(
                environ={ENV_PORT: "18080", ENV_TLS_PORT: "18443"},
                tls_enabled=True,
            ),
            (18080, 18443),
        )
        self.assertEqual(
            resolve_ports(
                environ={ENV_PORT: " 1 ", ENV_TLS_PORT: " 65535 "},
                tls_enabled=True,
            ),
            (1, 65535),
        )

    def test_invalid_non_numeric_values(self):
        for bad in ("abc", "-1", "+8080", "8_080", "80.5", "8080px"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    resolve_ports(environ={ENV_PORT: bad}, tls_enabled=False)
                self.assertIn(ENV_PORT, str(ctx.exception))

    def test_out_of_range_values(self):
        for bad in ("0", "65536", "99999"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    resolve_ports(environ={ENV_TLS_PORT: bad}, tls_enabled=False)
                self.assertIn(ENV_TLS_PORT, str(ctx.exception))

    def test_duplicate_http_and_tls_port_allowed_when_tls_disabled(self):
        self.assertEqual(
            resolve_ports(environ={ENV_PORT: "8443"}, tls_enabled=False),
            (8443, 8443),
        )
        self.assertEqual(
            resolve_ports(
                environ={ENV_PORT: "9000", ENV_TLS_PORT: "9000"},
                tls_enabled=False,
            ),
            (9000, 9000),
        )

    def test_duplicate_http_and_tls_port_rejected_when_tls_enabled(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_ports(environ={ENV_PORT: "8443"}, tls_enabled=True)
        self.assertIn("8443", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            resolve_ports(
                environ={ENV_PORT: "9000", ENV_TLS_PORT: "9000"},
                tls_enabled=True,
            )
        self.assertIn("9000", str(ctx.exception))


class TestServerPortStartup(unittest.TestCase):
    def test_server_exits_nonzero_on_invalid_port(self):
        env = os.environ.copy()
        env[ENV_PORT] = "not-a-port"
        env.pop(ENV_TLS_PORT, None)
        proc = subprocess.run(
            [sys.executable, "server.py"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(ENV_PORT, proc.stderr)

    def test_server_exits_nonzero_on_duplicate_port_when_tls_enabled(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            shutil.copy2(os.path.join(REPO_ROOT, "server.py"), os.path.join(tmpdir, "server.py"))
            shutil.copytree(
                os.path.join(REPO_ROOT, "server"),
                os.path.join(tmpdir, "server"),
                ignore=shutil.ignore_patterns("__pycache__", "test_*.py"),
            )
            certs_dir = os.path.join(tmpdir, "certs")
            os.makedirs(certs_dir)
            open(os.path.join(certs_dir, "tls-cert.pem"), "w").close()
            open(os.path.join(certs_dir, "tls-key.pem"), "w").close()

            env = os.environ.copy()
            env[ENV_PORT] = "8443"
            env.pop(ENV_TLS_PORT, None)
            proc = subprocess.run(
                [sys.executable, "server.py"],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("8443", proc.stderr)

    def test_server_starts_on_8443_when_tls_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.symlink(os.path.join(REPO_ROOT, "server.py"), os.path.join(tmpdir, "server.py"))
            os.symlink(os.path.join(REPO_ROOT, "server"), os.path.join(tmpdir, "server"))

            env = os.environ.copy()
            env[ENV_PORT] = "8443"
            env.pop(ENV_TLS_PORT, None)
            proc = subprocess.Popen(
                [sys.executable, "server.py"],
                cwd=tmpdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10.0
                ready = False
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        out = proc.stdout.read() if proc.stdout else ""
                        self.fail(f"server.py exited prematurely ({proc.returncode}): {out}")
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:8443/api/status", timeout=1.0) as resp:
                            if resp.status == 200:
                                ready = True
                                break
                    except Exception:
                        time.sleep(0.2)
                self.assertTrue(ready, "server.py did not become ready on port 8443")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                if proc.stdout:
                    proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
