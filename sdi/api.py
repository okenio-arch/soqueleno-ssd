from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, db_path
from .db import initialise, connect
from .registry import load_sources
from .profiles import get_profile
from .search import search_organisations, search_everything
from .engine import create_job, execute_job, start_scheduler, stop_scheduler
from .crawler import validate_public_url, CrawlBlocked

VERSION = "0.8.0"
WEB_DIR = Path(__file__).resolve().parent / "web"
app = FastAPI(title="Soqueleno Discovery Infrastructure", version=VERSION,
              description="Internal, focused web index for UK immigration legal-service organisations.")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


class SourceCreate(BaseModel):
    name: str = Field(min_length=3, max_length=160)
    url: str = Field(min_length=8, max_length=2048)
    category: str = Field(default="immigration_services_directory", max_length=100)
    reuse_basis: str = Field(min_length=12, max_length=500)
    terms_url: str | None = Field(default=None, max_length=2048)
    terms_reviewed: bool = False
    crawl_allowed: bool = False
    max_candidates_per_run: int = Field(default=20, ge=1, le=50)


class SourceStatus(BaseModel):
    status: str


def _scopes(authorization: str | None) -> tuple[str, set[str]]:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer API key obrigatória")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connect(db_path()) as conn:
        row = conn.execute("SELECT client_name,scopes FROM api_clients WHERE key_hash=? AND is_active=1", (digest,)).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Credencial inválida ou revogada")
        conn.execute("UPDATE api_clients SET last_used_at=CURRENT_TIMESTAMP WHERE key_hash=?", (digest,))
        return row["client_name"], set((row["scopes"] or "").split(","))


def authenticate(authorization: str | None, required_scope: str = "organisations:read") -> str:
    client, scopes = _scopes(authorization)
    if required_scope not in scopes:
        raise HTTPException(status_code=403, detail=f"A credencial não tem a permissão {required_scope}")
    return client


@app.on_event("startup")
def startup():
    initialise(db_path())
    load_sources(str(ROOT / "config" / "sources.json"), db_path())
    start_scheduler(db_path())


@app.on_event("shutdown")
def shutdown():
    stop_scheduler()


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health():
    return {"service": "soqueleno-sdi", "version": VERSION, "status": "ok", "mode": "focused_owned_index"}


@app.get("/api/v1/console/session")
def console_session(authorization: str | None = Header(default=None)):
    client, scopes = _scopes(authorization)
    return {"client": client, "scopes": sorted(scopes)}


@app.get("/api/v1/console/summary")
def console_summary(authorization: str | None = Header(default=None)):
    authenticate(authorization)
    with connect(db_path()) as conn:
        organisations = conn.execute("SELECT count(*) FROM organisations").fetchone()[0]
        pages = conn.execute("SELECT count(*) FROM crawled_pages WHERE is_indexable=1 AND crawl_status='success'").fetchone()[0]
        evidence = conn.execute("SELECT count(*) FROM evidence").fetchone()[0]
        sources = conn.execute("SELECT count(*) FROM sources WHERE status='enabled'").fetchone()[0]
        candidates = conn.execute("SELECT count(*) FROM discovered_urls WHERE status='candidate_only'").fetchone()[0]
        latest_run = conn.execute("SELECT run_id,started_at,finished_at,status,pages_checked,organisations_created,organisations_updated,datasets_imported,error FROM discovery_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        latest_job = conn.execute("SELECT job_id,status,created_at,started_at,finished_at,error FROM discovery_jobs ORDER BY job_id DESC LIMIT 1").fetchone()
    return {"organisations": organisations, "indexed_pages": pages, "evidence": evidence,
            "enabled_sources": sources, "external_candidates": candidates,
            "latest_run": dict(latest_run) if latest_run else None,
            "latest_job": dict(latest_job) if latest_job else None}


@app.get("/api/v1/search")
def search_all(q: str = Query(..., min_length=1), limit: int = Query(default=30, ge=1, le=100),
              city: str | None = None, regulator: str | None = None,
              authorization: str | None = Header(default=None)):
    authenticate(authorization)
    try:
        return search_everything(db_path(), q, limit=limit, city=city, regulator=regulator)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/v1/organisations/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(default=25, ge=1, le=100),
          city: str | None = None, regulator: str | None = None,
          authorization: str | None = Header(default=None)):
    authenticate(authorization)
    try:
        return {"items": search_organisations(db_path(), q, limit=limit, city=city, regulator=regulator)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/v1/organisations/{organisation_id}")
def profile(organisation_id: int, authorization: str | None = Header(default=None)):
    authenticate(authorization)
    item = get_profile(organisation_id, db_path())
    if not item:
        raise HTTPException(status_code=404, detail="Organização não encontrada")
    return item


@app.get("/api/v1/sources")
def sources(authorization: str | None = Header(default=None)):
    authenticate(authorization)
    with connect(db_path()) as conn:
        rows = conn.execute("""SELECT source_id,name,url,category,priority,access_method,status,source_type,
        last_success_at,last_error,records_seen,records_accepted,config_json FROM sources ORDER BY priority,source_id""").fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                cfg = json.loads(item.pop("config_json") or "{}")
            except ValueError:
                cfg = {}
            item["license"] = cfg.get("license")
            item["published_at"] = cfg.get("published_at")
            item["dataset_as_of"] = cfg.get("dataset_as_of")
            item["staleness_warning"] = cfg.get("staleness_warning")
            items.append(item)
        return {"items": items}


@app.post("/api/v1/sources", status_code=201)
def add_source(payload: SourceCreate, authorization: str | None = Header(default=None)):
    authenticate(authorization, "sources:manage")
    if not payload.terms_reviewed or not payload.crawl_allowed:
        raise HTTPException(status_code=400, detail="A fonte só pode ser ativada depois de rever os termos e confirmar que a recolha automatizada é permitida.")
    try:
        url = validate_public_url(payload.url)
    except CrawlBlocked as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    host = (urlparse(url).hostname or "").lower()
    source_id = "web_" + hashlib.sha256((host + url).encode("utf-8")).hexdigest()[:14]
    config = {
        "terms_reviewed": True, "crawl_allowed": True, "reuse_basis": payload.reuse_basis,
        "terms_url": payload.terms_url, "max_candidates_per_run": payload.max_candidates_per_run,
        "refresh_hours": 168, "license": payload.reuse_basis,
    }
    with connect(db_path()) as conn:
        conn.execute("""INSERT INTO sources(source_id,name,url,category,priority,access_method,status,notes,source_type,config_json)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET name=excluded.name,url=excluded.url,
        status='enabled',config_json=excluded.config_json,updated_at=CURRENT_TIMESTAMP""",
        (source_id, payload.name, url, payload.category, 20, "terms_reviewed_robots_checked_http", "enabled",
         "Source submitted in the local SDI console. Terms and reuse basis were explicitly confirmed by the operator.",
         "html_directory_seed", json.dumps(config, ensure_ascii=False)))
    return {"source_id": source_id, "name": payload.name, "url": url, "status": "enabled"}


@app.patch("/api/v1/sources/{source_id}/status")
def update_source_status(source_id: str, payload: SourceStatus, authorization: str | None = Header(default=None)):
    authenticate(authorization, "sources:manage")
    if payload.status not in {"enabled", "disabled", "blocked_pending_authorisation"}:
        raise HTTPException(status_code=400, detail="Estado inválido")
    with connect(db_path()) as conn:
        cur = conn.execute("UPDATE sources SET status=?,updated_at=CURRENT_TIMESTAMP WHERE source_id=?", (payload.status, source_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Fonte não encontrada")
    return {"source_id": source_id, "status": payload.status}


@app.post("/api/v1/discovery/run", status_code=202)
def run_discovery(background_tasks: BackgroundTasks, authorization: str | None = Header(default=None)):
    authenticate(authorization, "discovery:run")
    job_id = create_job(db_path())
    background_tasks.add_task(execute_job, job_id, db_path())
    return {"job_id": job_id, "status": "queued", "message": "Ciclo completo de descoberta colocado na fila."}


@app.get("/api/v1/discovery/jobs/{job_id}")
def discovery_job(job_id: int, authorization: str | None = Header(default=None)):
    authenticate(authorization)
    with connect(db_path()) as conn:
        row = conn.execute("SELECT * FROM discovery_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Execução não encontrada")
    item = dict(row)
    if item.get("result_json"):
        try:
            item["result"] = json.loads(item.pop("result_json"))
        except ValueError:
            pass
    return item


@app.get("/api/v1/discovery/runs")
def discovery_runs(limit: int = Query(default=20, ge=1, le=100), authorization: str | None = Header(default=None)):
    authenticate(authorization)
    with connect(db_path()) as conn:
        rows = conn.execute("""SELECT run_id,seed_url,source_id,run_type,started_at,finished_at,pages_checked,
        candidates_found,organisations_created,organisations_updated,datasets_imported,status,error
        FROM discovery_runs ORDER BY run_id DESC LIMIT ?""", (limit,)).fetchall()
        return {"items": [dict(row) for row in rows]}
