"""
Production/development authentication mode separation tests (Phase 9;
plan.md Step 9.11): src/api/server.py's AUTH_MODE environment variable.

Run in subprocesses (not via a plain `import server` in-process) because
AUTH_MODE is read and the AuthenticationProvider is constructed at
module import time -- the exact fail-closed behavior being tested here.
Re-importing an already-cached `server` module in-process would not
exercise that import-time code path a second time.

Run with:
    python -m unittest tests.test_auth_mode_separation -v
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_IMPORT_SNIPPET = (
    "import sys; "
    "sys.path.insert(0, 'src/api'); "
    "sys.path.insert(0, 'src/agent'); "
    "import server; "
    "print('IMPORTED:' + type(server._authentication_provider).__name__)"
)


def _run(env_overrides: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", _IMPORT_SNIPPET],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60,
    )


class TestAuthModeSeparation(unittest.TestCase):
    def test_default_mode_uses_development_provider(self):
        result = _run({"AUTH_MODE": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("IMPORTED:DevelopmentAuthenticationProvider", result.stdout)

    def test_dev_mode_explicit_uses_development_provider(self):
        result = _run({"AUTH_MODE": "dev"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("IMPORTED:DevelopmentAuthenticationProvider", result.stdout)

    def test_production_mode_without_config_fails_closed_at_startup(self):
        """plan.md Principle 32/33: production must never silently fall back to the development provider."""
        env = {"AUTH_MODE": "production", "OIDC_ISSUER_URL": "", "OIDC_AUDIENCE": "", "OIDC_JWKS_URL": ""}
        result = _run(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AuthConfigurationError", result.stderr)
        self.assertNotIn("IMPORTED:DevelopmentAuthenticationProvider", result.stdout)
        self.assertNotIn("IMPORTED", result.stdout)

    def test_oidc_alias_without_config_also_fails_closed(self):
        env = {"AUTH_MODE": "oidc", "OIDC_ISSUER_URL": "", "OIDC_AUDIENCE": "", "OIDC_JWKS_URL": ""}
        result = _run(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AuthConfigurationError", result.stderr)

    def test_production_mode_with_valid_env_config_uses_oidc_provider(self):
        env = {
            "AUTH_MODE": "production",
            "OIDC_ISSUER_URL": "https://issuer.example.test/",
            "OIDC_AUDIENCE": "test-api",
            "OIDC_JWKS_URL": "https://issuer.example.test/jwks.json",
        }
        result = _run(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("IMPORTED:OIDCAuthenticationProvider", result.stdout)

    def test_production_mode_never_falls_back_even_partial_config(self):
        """Only issuer configured, audience/jwks_url still missing -- must still fail closed, not partially proceed."""
        env = {
            "AUTH_MODE": "production",
            "OIDC_ISSUER_URL": "https://issuer.example.test/",
            "OIDC_AUDIENCE": "",
            "OIDC_JWKS_URL": "",
        }
        result = _run(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AuthConfigurationError", result.stderr)


if __name__ == "__main__":
    unittest.main()
