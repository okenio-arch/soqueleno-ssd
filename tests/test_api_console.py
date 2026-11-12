import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdi.db import initialise, connect
from sdi.api import app, WEB_DIR


class ApiConsoleTests(unittest.TestCase):
    def test_console_static_asset_exists(self):
        self.assertTrue((WEB_DIR / "index.html").is_file())
        self.assertTrue(any(getattr(route, "path", None) == "/" for route in app.routes))

    def test_console_key_can_receive_explicit_scopes(self):
        import main
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "sdi.sqlite3")
            with patch("sys.argv", [
                "main.py", "create-key", "--db", db, "--client", "sdi-console",
                "--scopes", "organisations:read,discovery:run,sources:manage",
            ]):
                main.main()
            initialise(db)
            with connect(db) as conn:
                row = conn.execute(
                    "SELECT scopes FROM api_clients WHERE client_name='sdi-console'"
                ).fetchone()
            self.assertEqual(row["scopes"], "organisations:read,discovery:run,sources:manage")

if __name__ == "__main__":
    unittest.main()
