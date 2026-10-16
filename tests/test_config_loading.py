from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class DotenvConfigTest(unittest.TestCase):
    def load_config(self, dotenv_text, local_text=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(Path(__file__).resolve().parents[1] / "config.py", root / "config.py")
            (root / ".env").write_text(dotenv_text, encoding="utf-8")
            (root / ".env.local").write_text(local_text, encoding="utf-8")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("NOVIIS_", "CHALLENGE_", "MCP_SERVER_", "LOG_")) and key != "LOCK_FILE_PATH"}
            return subprocess.run(
                [sys.executable, "-c", "import config; print(config.APP_ENV, config.CHALLENGE_EXPIRES_SECONDS)"],
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



if __name__ == "__main__":
    unittest.main()
