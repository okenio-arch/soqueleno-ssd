import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdi.db import initialise, connect
from fastapi import HTTPException

from sdi.api import app, WEB_DIR, SourceStatus, update_source_status


class ApiConsoleTests(unittest.TestCase):
    def test_console_static_asset_exists(self):
        self.assertTrue((WEB_DIR / "index.html").is_file())
        self.assertTrue(any(getattr(route, "path", None) == "/" for route in app.routes))

    def test_source_without_adapter_cannot_be_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "sdi.sqlite3")
            initialise(db)
            with connect(db) as conn:
                conn.execute("""INSERT INTO sources(source_id,name,url,category,access_method,status,source_type)
                    VALUES('sra','SRA API','https://example.org','regulator','api_key','blocked_pending_authorisation','manual_or_authorisation_pending')""")
            with patch("sdi.api.db_path", return_value=db), patch("sdi.api.authenticate", return_value="console"):
                with self.assertRaises(HTTPException) as error:
                    update_source_status("sra", SourceStatus(status="enabled"), authorization="Bearer test")
            self.assertEqual(error.exception.status_code, 400)

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
