from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config  # noqa: E402


class ConfigFileTests(unittest.TestCase):
    def test_user_config_takes_precedence_over_legacy_project_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            user_env = root / "user.env"
            legacy_env = root / "legacy.env"
            user_env.write_text("DEEPSEEK_API_KEY=user-key-12345\n", encoding="utf-8")
            legacy_env.write_text("DEEPSEEK_API_KEY=legacy-key-12345\n", encoding="utf-8")

            with (
                patch.object(config, "DEFAULT_ENV_FILE", user_env),
                patch.object(config, "LEGACY_ENV_FILE", legacy_env),
                patch.dict("os.environ", {}, clear=True),
            ):
                loaded = config.load_config()

            self.assertEqual(loaded.api_key, "user-key-12345")

    def test_legacy_project_config_is_used_during_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            user_env = root / "missing-user.env"
            legacy_env = root / "legacy.env"
            legacy_env.write_text("DEEPSEEK_API_KEY=legacy-key-12345\n", encoding="utf-8")

            with (
                patch.object(config, "DEFAULT_ENV_FILE", user_env),
                patch.object(config, "LEGACY_ENV_FILE", legacy_env),
                patch.dict("os.environ", {}, clear=True),
            ):
                loaded = config.load_config()

            self.assertEqual(loaded.api_key, "legacy-key-12345")


if __name__ == "__main__":
    unittest.main()
