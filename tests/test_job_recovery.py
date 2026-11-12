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
                conn.execute("""INSERT INTO discovered_urls
                    (parent_url,discovered_url,domain,discovery_type,status,source_id,depth,attempts)
                    VALUES('https://directory.example','https://directory.example/page-2','directory.example',
                    'seed_page','processing','seed',1,1)""")
                conn.execute("""INSERT INTO discovered_urls
                    (parent_url,discovered_url,domain,discovery_type,status,source_id,depth,attempts)
                    VALUES('https://directory.example','https://directory.example/page-3','directory.example',
                    'seed_page','processing','seed',1,3)""")
                run_id = conn.execute(
                    """INSERT INTO discovery_runs(seed_url,run_type,status,started_at)
                    VALUES('sdi://test','full_discovery','running',datetime('now','-1 hour'))"""
                ).lastrowid
            result = recover_interrupted_work(db)
            with connect(db) as conn:
                job = conn.execute("SELECT status,error FROM discovery_jobs WHERE job_id=?", (job_id,)).fetchone()
                run = conn.execute("SELECT status,error FROM discovery_runs WHERE run_id=?", (run_id,)).fetchone()
                frontier = conn.execute("SELECT status FROM discovered_urls WHERE discovered_url=?", ("https://directory.example/page-2",)).fetchone()
                exhausted = conn.execute("SELECT status FROM discovered_urls WHERE discovered_url=?", ("https://directory.example/page-3",)).fetchone()
            self.assertEqual(result, {"jobs_interrupted": 1, "runs_interrupted": 1, "frontier_requeued": 1, "frontier_exhausted": 1})
            self.assertEqual(job["status"], "interrupted")
            self.assertEqual(run["status"], "interrupted")
            self.assertTrue(job["error"])
            self.assertTrue(run["error"])
            self.assertEqual(frontier["status"], "queued")
            self.assertEqual(exhausted["status"], "failed")

if __name__ == "__main__":
    unittest.main()
