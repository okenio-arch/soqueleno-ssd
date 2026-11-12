import tempfile
import unittest
from pathlib import Path

from sdi.db import connect, initialise
from sdi.engine import recover_interrupted_work


class InterruptedWorkTests(unittest.TestCase):
    def test_stale_jobs_and_runs_are_marked_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "sdi.sqlite3")
            initialise(db)
            with connect(db) as conn:
                job_id = conn.execute(
                    """INSERT INTO discovery_jobs(job_type,status,created_at,started_at)
                    VALUES('full_discovery','running',datetime('now','-1 hour'),datetime('now','-1 hour'))"""
                ).lastrowid
                run_id = conn.execute(
                    """INSERT INTO discovery_runs(seed_url,run_type,status,started_at)
                    VALUES('sdi://test','full_discovery','running',datetime('now','-1 hour'))"""
                ).lastrowid
            result = recover_interrupted_work(db)
            with connect(db) as conn:
                job = conn.execute("SELECT status,error FROM discovery_jobs WHERE job_id=?", (job_id,)).fetchone()
                run = conn.execute("SELECT status,error FROM discovery_runs WHERE run_id=?", (run_id,)).fetchone()
            self.assertEqual(result, {"jobs_interrupted": 1, "runs_interrupted": 1})
            self.assertEqual(job["status"], "interrupted")
            self.assertEqual(run["status"], "interrupted")
            self.assertTrue(job["error"])
            self.assertTrue(run["error"])

if __name__ == "__main__":
    unittest.main()
