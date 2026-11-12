from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, setting
from .db import connect, initialise
from .registry import load_sources, get_source_config
from .datasets import collect_dataset_source
from .recursive_discovery import discover_recursive_from_seed
from .crawler import crawl_organisation

_ENGINE_LOCK = threading.Lock()
_SCHEDULER_STOP = threading.Event()
_SCHEDULER_THREAD: threading.Thread | None = None


def _source_object(row) -> dict:
    return {
        "source_id": row["source_id"], "name": row["name"], "url": row["url"],
        "category": row["category"], "priority": row["priority"],
        "access_method": row["access_method"], "status": row["status"],
        "source_type": row["source_type"], "config": get_source_config(row),
    }


def _source_due(conn, source_id: str, refresh_hours: int) -> bool:
    row = conn.execute(
        "SELECT last_success_at,CASE WHEN last_success_at IS NULL THEN 999999 ELSE (julianday('now')-julianday(last_success_at))*24 END AS age_hours FROM sources WHERE source_id=?",
        (source_id,),
    ).fetchone()
    return not row or row["last_success_at"] is None or float(row["age_hours"] or 0) >= max(1, refresh_hours)


def run_discovery_cycle(db_path: str | None = None, *, force: bool = False, wait_for_lock: bool = False) -> dict:
    """Run the complete focused discovery pipeline from the built-in source registry.

    No third-party search API is used. Official datasets are downloaded directly from
    GOV.UK publication pages, then the published organisation websites are crawled.
    """
    db_path = db_path or str(ROOT / "data" / "sdi.sqlite3")
    acquired = _ENGINE_LOCK.acquire(timeout=30) if wait_for_lock else _ENGINE_LOCK.acquire(blocking=False)
    if not acquired:
        return {"status": "already_running", "message": "Uma execução da SDI já está em curso."}
    started = time.monotonic()
    report = {
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
        "sources_considered": 0, "sources_run": 0, "datasets_imported": 0,
        "records_seen": 0, "records_accepted": 0, "organisations_created": 0,
        "organisations_updated": 0, "websites_crawled": 0, "pages_indexed": 0,
        "external_candidates_recorded": 0, "seed_pages_checked": 0,
        "recursive_domains_processed": 0, "seed_candidates_found": 0, "seed_candidates_accepted": 0, "seed_candidates_rejected": 0,
        "results": [], "errors": [],
    }
    run_id = None
    try:
        initialise(db_path)
        load_sources(str(ROOT / "config" / "sources.json"), db_path)
        with connect(db_path) as conn:
            run_id = conn.execute(
                "INSERT INTO discovery_runs(seed_url,run_type,status) VALUES('sdi://full-discovery-cycle','full_discovery','running')"
            ).lastrowid
            source_rows = conn.execute("SELECT * FROM sources ORDER BY priority,source_id").fetchall()
        report["sources_considered"] = len(source_rows)
        timeout = int(setting("SDI_REQUEST_TIMEOUT", "20"))
        for row in source_rows:
            source = _source_object(row)
            sid = source["source_id"]
            if source["status"] not in {"enabled", "available_with_freshness_warning"}:
                report["results"].append({"source_id": sid, "name": source["name"], "status": "skipped", "reason": source["status"]})
                continue
            cfg = source["config"]
            refresh_hours = int(cfg.get("refresh_hours", 720))
            with connect(db_path) as conn:
                due = force or _source_due(conn, sid, refresh_hours)
            if not due:
                report["results"].append({"source_id": sid, "name": source["name"], "status": "fresh", "reason": f"Atualizada há menos de {refresh_hours} horas"})
                continue
            try:
                if source["source_type"] == "govuk_xlsx":
                    result = collect_dataset_source(source, db_path, timeout=timeout)
                    seen = int(result.get("rows_seen", 0))
                    accepted = int(result.get("rows_valid", 0))
                    created = int(result.get("created", 0))
                    updated = int(result.get("updated", 0))
                    with connect(db_path) as conn:
                        conn.execute(
                            """UPDATE sources SET last_success_at=CURRENT_TIMESTAMP,last_error=NULL,
                            records_seen=records_seen+?,records_accepted=records_accepted+?,updated_at=CURRENT_TIMESTAMP WHERE source_id=?""",
                            (seen, accepted, sid),
                        )
                    report["datasets_imported"] += 1
                    report["records_seen"] += seen
                    report["records_accepted"] += accepted
                    report["organisations_created"] += created
                    report["organisations_updated"] += updated
                    report["sources_run"] += 1
                    report["results"].append({"source_id": sid, "name": source["name"], "status": "completed", **result})
                elif source["source_type"] == "html_directory_seed":
                    if not (cfg.get("terms_reviewed") and cfg.get("crawl_allowed") and cfg.get("reuse_basis")):
                        report["results"].append({"source_id": sid, "name": source["name"], "status": "skipped", "reason": "A fonte precisa de base de reutilização e revisão de termos antes da recolha"})
                        continue
                    result = discover_recursive_from_seed(
                        source["url"], db_path, source_id=sid,
                        max_depth=int(cfg.get("recursive_max_depth", setting("SDI_RECURSIVE_MAX_DEPTH", "2"))),
                        max_domains=int(cfg.get("recursive_max_domains", setting("SDI_RECURSIVE_MAX_DOMAINS", "30"))),
                        max_candidates_per_seed=int(cfg.get("max_candidates_per_run", 20)),
                        timeout=timeout,
                        delay=float(setting("SDI_REQUEST_DELAY_SECONDS", "1.0")),
                    )
                    source_errors = result.get("errors", [])
                    source_ok = result.get("status", "completed") == "completed" and not source_errors
                    with connect(db_path) as conn:
                        if source_ok:
                            conn.execute("UPDATE sources SET last_success_at=CURRENT_TIMESTAMP,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE source_id=?", (sid,))
                        else:
                            conn.execute("UPDATE sources SET last_error=?,updated_at=CURRENT_TIMESTAMP WHERE source_id=?",
                                         (json.dumps(source_errors[:5], ensure_ascii=False)[:1000] or result.get("status", "incomplete"), sid))
                    report["sources_run"] += 1
                    report["seed_pages_checked"] += int(result.get("pages_checked", 0))
                    report["recursive_domains_processed"] += int(result.get("domains_processed", 0))
                    report["seed_candidates_found"] += int(result.get("candidates_found", 0))
                    report["seed_candidates_accepted"] += int(result.get("candidates_accepted", 0))
                    report["seed_candidates_rejected"] += int(result.get("candidates_rejected", 0))
                    report["organisations_created"] += int(result.get("organisations_created", 0))
                    report["organisations_updated"] += int(result.get("organisations_updated", 0))
                    report["errors"].extend({"source_id": sid, **item} for item in source_errors[:20])
                    report["results"].append({"source_id": sid, "name": source["name"], **result})
                else:
                    report["results"].append({"source_id": sid, "name": source["name"], "status": "skipped", "reason": "Adaptador ainda não implementado"})
            except Exception as exc:
                message = str(exc)[:500]
                with connect(db_path) as conn:
                    conn.execute("UPDATE sources SET last_error=?,updated_at=CURRENT_TIMESTAMP WHERE source_id=?", (message, sid))
                report["errors"].append({"source_id": sid, "error": message})
                report["results"].append({"source_id": sid, "name": source["name"], "status": "failed", "error": message})

        # Adaptive website queue: incomplete and never-crawled profiles go first.
        max_sites = max(1, min(int(setting("SDI_MAX_WEBSITES_PER_CYCLE", "20")), 200))
        max_pages = max(1, min(int(setting("SDI_MAX_PAGES_PER_SITE", "8")), 30))
        max_depth = max(0, min(int(setting("SDI_MAX_DEPTH", "2")), 5))
        refresh_days = max(1, int(setting("SDI_WEBSITE_REFRESH_DAYS", "30")))
        with connect(db_path) as conn:
            targets = conn.execute(
                """SELECT organisation_id,website FROM organisations
                WHERE website IS NOT NULL AND trim(website)<>''
                AND (website_last_attempt_at IS NULL OR (julianday('now')-julianday(website_last_attempt_at))>=?)
                ORDER BY (CASE WHEN public_email IS NULL OR public_email='' THEN 1 ELSE 0 END +
                          CASE WHEN phone IS NULL OR phone='' THEN 1 ELSE 0 END +
                          CASE WHEN immigration_services IS NULL OR immigration_services='' THEN 1 ELSE 0 END) DESC,
                         coalesce(website_last_attempt_at,'1900-01-01') ASC, organisation_id ASC LIMIT ?""",
                (refresh_days, max_sites),
            ).fetchall()
        for target in targets:
            try:
                result = crawl_organisation(
                    int(target["organisation_id"]), target["website"], db_path,
                    max_pages=max_pages, max_depth=max_depth, timeout=timeout,
                    delay=float(setting("SDI_REQUEST_DELAY_SECONDS", "0.8")),
                )
                report["websites_crawled"] += 1
                report["pages_indexed"] += int(result.get("pages_crawled", 0))
                report["external_candidates_recorded"] += int(result.get("external_candidates_recorded", 0))
                if result.get("errors"):
                    report["errors"].extend({"organisation_id": target["organisation_id"], **x} for x in result["errors"][:5])
            except Exception as exc:
                with connect(db_path) as conn:
                    conn.execute("UPDATE organisations SET website_last_attempt_at=CURRENT_TIMESTAMP,website_crawl_status='failed' WHERE organisation_id=?",
                                 (target["organisation_id"],))
                report["errors"].append({"organisation_id": target["organisation_id"], "error": str(exc)[:300]})
        report["status"] = "completed_with_errors" if report["errors"] else "completed"
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        if run_id:
            with connect(db_path) as conn:
                conn.execute(
                    """UPDATE discovery_runs SET finished_at=CURRENT_TIMESTAMP,pages_checked=?,candidates_found=?,
                    organisations_created=?,organisations_updated=?,datasets_imported=?,status=?,error=? WHERE run_id=?""",
                    (report["pages_indexed"], report["external_candidates_recorded"], report["organisations_created"],
                     report["organisations_updated"], report["datasets_imported"], report["status"],
                     json.dumps(report["errors"][:10], ensure_ascii=False) if report["errors"] else None, run_id),
                )
        report["run_id"] = run_id
        return report
    except Exception as exc:
        report["status"] = "failed"
        report["errors"].append({"engine": str(exc)[:500]})
        if run_id:
            with connect(db_path) as conn:
                conn.execute("UPDATE discovery_runs SET finished_at=CURRENT_TIMESTAMP,status='failed',error=? WHERE run_id=?", (str(exc)[:500], run_id))
        return report
    finally:
        _ENGINE_LOCK.release()


def create_job(db_path: str | None = None) -> int:
    db_path = db_path or str(ROOT / "data" / "sdi.sqlite3")
    initialise(db_path)
    with connect(db_path) as conn:
        return int(conn.execute("INSERT INTO discovery_jobs(job_type,status) VALUES('full_discovery','queued')").lastrowid)


def execute_job(job_id: int, db_path: str | None = None) -> None:
    db_path = db_path or str(ROOT / "data" / "sdi.sqlite3")
    with connect(db_path) as conn:
        conn.execute("UPDATE discovery_jobs SET status='running',started_at=CURRENT_TIMESTAMP WHERE job_id=? AND status='queued'", (job_id,))
        row = conn.execute("SELECT status FROM discovery_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row or row[0] != "running":
            return
    result = run_discovery_cycle(db_path, wait_for_lock=True)
    with connect(db_path) as conn:
        conn.execute("UPDATE discovery_jobs SET status=?,finished_at=CURRENT_TIMESTAMP,result_json=?,error=? WHERE job_id=?",
                     (result.get("status", "failed"), json.dumps(result, ensure_ascii=False),
                      json.dumps(result.get("errors", []), ensure_ascii=False)[:2000] if result.get("errors") else None, job_id))


def start_scheduler(db_path: str | None = None) -> None:
    global _SCHEDULER_THREAD
    if setting("SDI_SCHEDULER_ENABLED", "true").casefold() not in {"1", "true", "yes", "on"}:
        return
    if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
        return
    _SCHEDULER_STOP.clear()
    db_path = db_path or str(ROOT / "data" / "sdi.sqlite3")

    def loop():
        run_on_start = setting("SDI_AUTO_RUN_ON_START", "true").casefold() in {"1", "true", "yes", "on"}
        interval_hours = max(1, int(setting("SDI_SCHEDULE_HOURS", "24")))
        first = True
        while not _SCHEDULER_STOP.is_set():
            if first and not run_on_start:
                first = False
            else:
                try:
                    job = create_job(db_path)
                    execute_job(job, db_path)
                except Exception:
                    pass
            first = False
            if _SCHEDULER_STOP.wait(interval_hours * 3600):
                break

    _SCHEDULER_THREAD = threading.Thread(target=loop, name="sdi-discovery-scheduler", daemon=True)
    _SCHEDULER_THREAD.start()


def stop_scheduler() -> None:
    _SCHEDULER_STOP.set()
