"""johto/lane_login.py: a new lane gets Austin's token as its Claude login."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from johto import lane_login as L

TOKEN = "sk-ant-oat01-" + "a" * 90
CONTAINER = "prework-PREWORK_abc12345-0123456789abcdef-model_a"


class Seed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.cfg = self.home / "cc-config" / "PREWORK_abc12345-0123456789abcdef_model_a"
        self.cfg.mkdir(parents=True)
        self.run = root / "run"
        self.run.mkdir()
        (self.run / "prework-manifest.json").write_text(json.dumps({"lane": {"container": CONTAINER}}))

    def token(self, text=TOKEN):
        (self.home / L.TOKEN_FILE).write_text(text + "\n")

    def test_writes_an_owner_only_login_holding_the_token(self):
        self.token()
        status, _ = L.seed(self.run, self.home)
        self.assertEqual(status, "seeded")
        f = self.cfg / L.LOGIN_FILE
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        o = json.loads(f.read_text())["claudeAiOauth"]
        self.assertEqual(o["accessToken"], TOKEN)
        self.assertIsNone(o["refreshToken"])
        self.assertEqual(o["subscriptionType"], "max")
        self.assertGreater(o["expiresAt"] / 1000, __import__("time").time() + 300 * 86400)

    def test_never_overwrites_a_login(self):
        self.token()
        (self.cfg / L.LOGIN_FILE).write_text('{"claudeAiOauth": {"accessToken": "from /login"}}')
        self.assertEqual(L.seed(self.run, self.home)[0], "kept")
        self.assertIn("from /login", (self.cfg / L.LOGIN_FILE).read_text())

    def test_no_or_bad_token_does_nothing(self):
        self.assertEqual(L.seed(self.run, self.home)[0], "no_token")
        self.token("not a token")
        self.assertEqual(L.seed(self.run, self.home)[0], "no_token")
        self.assertFalse((self.cfg / L.LOGIN_FILE).exists())

    def test_no_manifest_or_config_folder(self):
        self.token()
        (self.run / "prework-manifest.json").unlink()
        self.assertEqual(L.seed(self.run, self.home)[0], "no_lane")
        (self.run / "prework-manifest.json").write_text(json.dumps({"lane": {"container": "other-x-model_a"}}))
        self.assertEqual(L.seed(self.run, self.home)[0], "no_lane")

    def test_the_message_never_holds_the_token(self):
        self.token()
        for _ in range(2):
            _, msg = L.seed(self.run, self.home)
            self.assertNotIn(TOKEN, msg)


if __name__ == "__main__":
    unittest.main()
