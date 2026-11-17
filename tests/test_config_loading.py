from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class DotenvConfigTest(unittest.TestCase):
    def load_config(self, dotenv_text, local_text="", expression="config.APP_ENV, config.CHALLENGE_EXPIRES_SECONDS"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(Path(__file__).resolve().parents[1] / "config.py", root / "config.py")
            (root / ".env").write_text(dotenv_text, encoding="utf-8")
            (root / ".env.local").write_text(local_text, encoding="utf-8")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("NOVIIS_", "CHALLENGE_", "MCP_", "LOG_", "BOARDS_")) and key != "LOCK_FILE_PATH"}
            return subprocess.run(
                [sys.executable, "-c", f"import config; print({expression})"],
                cwd=root, env=env, text=True, capture_output=True, check=False,
            )

    def test_production_mode_in_dotenv_enforces_required_values(self):
        result = self.load_config("NOVIIS_ENV=production\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing required environment variable: NOVIIS_API_BASE_URL", result.stderr)

    def test_local_overrides_are_applied_before_selecting_mode(self):
        result = self.load_config("NOVIIS_ENV=production\n", "NOVIIS_ENV=development\nCHALLENGE_EXPIRES_SECONDS=180\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "development 180")

    def test_default_expiry_and_invalid_policy(self):
        result = self.load_config("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "development 120")
        result = self.load_config("CHALLENGE_MAX_PENDING=0\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CHALLENGE_MAX_PENDING must be a positive integer", result.stderr)

    def test_response_byte_budget_must_be_positive(self):
        result = self.load_config("MCP_MAX_RESPONSE_BYTES=0\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MCP_MAX_RESPONSE_BYTES must be a positive integer", result.stderr)

    def test_boolean_spellings_and_invalid_values(self):
        for name in ("LOG_JSON", "NOVIIS_OPERATION_SUPPORT"):
            for value in ("true", "YES", "On", "1", "false", "NO", "Off", "0"):
                with self.subTest(name=name, value=value):
                    result = self.load_config(f'{name}=" {value} "\n', expression=f"config.{name}")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), str(value.lower() in {"true", "yes", "on", "1"}))
            for value in ("treu", "", "2"):
                with self.subTest(name=name, value=value):
                    result = self.load_config(f"{name}={value}\n")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"{name} must be a boolean", result.stderr)

    def test_port_and_cache_ttl_bounds(self):
        for name, values in (("MCP_SERVER_PORT", ("0", "-1", "65536")),
                             ("BOARDS_CACHE_TTL_SECONDS", ("0", "-1", "invalid"))):
            for value in values:
                with self.subTest(name=name, value=value):
                    result = self.load_config(f"{name}={value}\n")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)
        for port in (1, 65535):
            result = self.load_config(f"MCP_SERVER_PORT={port}\nBOARDS_CACHE_TTL_SECONDS=1\n")
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
