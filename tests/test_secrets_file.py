#!/usr/bin/env python3
"""6.1.5: config/secrets.env is read into the environment at startup. Keys stay environment variables;
nothing in the code holds a value. Offline."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import gold_monitor as M  # noqa: E402


class SecretsFileTests(unittest.TestCase):
    def write(self, text):
        p = Path(tempfile.mkdtemp()) / "secrets.env"
        p.write_text(text, encoding="utf-8")
        return p

    def test_formats_comments_quotes_and_setx_lines(self):
        p = self.write('# comment\n\nANTHROPIC_API_KEY=sk-ant-usr-abc\n'
                       'TELEGRAM_BOT_TOKEN "123:AAA"\n'
                       'setx TELEGRAM_CHAT_SIGNALS "@XAU_USD_Bot_Trading"\n'
                       "OPENAI_API_KEY='sk-proj-x'\n"
                       'MT5_PATH=C:\\Program Files\\MetaTrader 5\\terminal64.exe\n'
                       'this line is wrong\n')
        with patch.dict(os.environ, {}, clear=False):
            for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_SIGNALS", "OPENAI_API_KEY", "MT5_PATH"):
                os.environ.pop(k, None)
            loaded = M.load_secrets(p, log=False)
            self.assertEqual(loaded, ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_SIGNALS",
                                      "OPENAI_API_KEY", "MT5_PATH"])
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-usr-abc")
            self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "123:AAA")
            self.assertEqual(os.environ["TELEGRAM_CHAT_SIGNALS"], "@XAU_USD_Bot_Trading")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-proj-x")
            self.assertEqual(os.environ["MT5_PATH"], "C:\\Program Files\\MetaTrader 5\\terminal64.exe")
            self.assertNotIn("sk-ant-usr-abc", M.safe_error("key sk-ant-usr-abc rejected"))   # redaction follows

    def test_file_wins_over_a_stale_setx_value_unless_told_otherwise(self):
        """22 Oct: a revoked key stayed in the Windows environment for hours; the file is the one source."""
        p = self.write("ANTHROPIC_API_KEY=new-key\n")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "old-key"}):
            M.load_secrets(p, log=False)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "new-key")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "old-key"}):
            M.load_secrets(p, override=False, log=False)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "old-key")

    def test_missing_file_and_env_pointer(self):
        with patch.dict(os.environ, {"GOLD_SECRETS_FILE": str(Path(tempfile.mkdtemp()) / "none.env")}):
            self.assertEqual(M.load_secrets(log=False), [])
        p = self.write("TELEGRAM_CHAT_LOG=@XAU_USD_Bot_Log\n")
        with patch.dict(os.environ, {"GOLD_SECRETS_FILE": str(p), "TELEGRAM_CHAT_LOG": ""}):
            self.assertEqual(M.load_secrets(log=False), ["TELEGRAM_CHAT_LOG"])
            self.assertEqual(os.environ["TELEGRAM_CHAT_LOG"], "@XAU_USD_Bot_Log")

    def test_example_file_ships_and_real_file_is_ignored_by_git(self):
        self.assertTrue((ROOT / "config" / "secrets.env.example").exists())
        self.assertIn("config/secrets.env", (ROOT / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
