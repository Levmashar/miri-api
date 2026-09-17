import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.config import Config
from src.core.runtime_settings import RuntimeSettings


class RuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_env_defaults_load_and_updates_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            with patch.object(Config, "NEW_CHAT_EVERY_REQUEST", False), \
                 patch.object(Config, "TEMPORARY_CHATS", True):
                settings = RuntimeSettings(path)
                await settings.load()
                self.assertEqual(settings.snapshot(), {
                    "new_chat_every_request": False,
                    "temporary_chats": True,
                })
                saved = await settings.update({"new_chat_every_request": True})
                self.assertTrue(Config.NEW_CHAT_EVERY_REQUEST)
                self.assertEqual(json.loads(path.read_text()), saved)

                reloaded = RuntimeSettings(path)
                await reloaded.load()
                self.assertEqual(reloaded.snapshot(), saved)

    async def test_rejects_unknown_or_non_boolean_values_without_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = RuntimeSettings(Path(tmp) / "settings.json")
            original = settings.snapshot()
            with self.assertRaises(ValueError):
                await settings.update({"temporary_chats": 1})
            with self.assertRaises(ValueError):
                await settings.update({"not_a_setting": True})
            self.assertEqual(settings.snapshot(), original)


if __name__ == "__main__":
    unittest.main()
