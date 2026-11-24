import os
import subprocess
import sys
import unittest

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
        self.assertEqual(resolve_ports(environ={}), (8080, 8443))

    def test_defaults_when_empty_or_whitespace(self):
        env = {ENV_PORT: "", ENV_TLS_PORT: "   "}
        self.assertEqual(resolve_ports(environ=env), (8080, 8443))

    def test_valid_custom_ports(self):
        self.assertEqual(
            resolve_ports(environ={ENV_PORT: "18080", ENV_TLS_PORT: "18443"}),
            (18080, 18443),
        )
        self.assertEqual(
            resolve_ports(environ={ENV_PORT: " 1 ", ENV_TLS_PORT: " 65535 "}),
            (1, 65535),
        )

    def test_invalid_non_numeric_values(self):
        for bad in ("abc", "-1", "+8080", "8_080", "80.5", "8080px"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    resolve_ports(environ={ENV_PORT: bad})
                self.assertIn(ENV_PORT, str(ctx.exception))

    def test_out_of_range_values(self):
        for bad in ("0", "65536", "99999"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    resolve_ports(environ={ENV_TLS_PORT: bad})
                self.assertIn(ENV_TLS_PORT, str(ctx.exception))

    def test_duplicate_http_and_tls_port(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_ports(environ={ENV_PORT: "8443"})
        self.assertIn("8443", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            resolve_ports(environ={ENV_PORT: "9000", ENV_TLS_PORT: "9000"})
        self.assertIn("9000", str(ctx.exception))


class TestServerFailClosedOnInvalidPort(unittest.TestCase):
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

    def test_server_exits_nonzero_on_duplicate_port(self):
        env = os.environ.copy()
        env[ENV_PORT] = "9090"
        env[ENV_TLS_PORT] = "9090"
        proc = subprocess.run(
            [sys.executable, "server.py"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("9090", proc.stderr)


if __name__ == "__main__":
    unittest.main()
