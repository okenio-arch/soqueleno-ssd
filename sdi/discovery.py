from __future__ import annotations

import hashlib
import time
from urllib.parse import urljoin, urlparse, urldefrag

import httpx
from bs4 import BeautifulSoup

from .crawler import CrawlBlocked, USER_AGENT, ROBOT_TOKEN, _robots_allows, validate_public_url, bounded_get
from .db import connect
from .normalise import clean, domain_of, normalise_name

SOURCE_ID = "discovery_web"
IMMIGRATION_TERMS = (
    "immigration", "immigration law", "immigration solicitor", "immigration advice",
    "visa", "visas", "skilled worker", "family visa", "asylum", "ukvi",
    "indefinite leave to remain", "ilr", "settlement", "sponsor licence",
)
BUSINESS_TERMS = ("solicitor", "law firm", "legal services", "immigration adviser", "immigration advice", "lawyers")
EXCLUDED_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "youtube.com", "tiktok.com", "wikipedia.org", "google.com", "bing.com",
}


def relevance(title: str, description: str, text: str) -> tuple[float, list[str]]:
    corpus = f"{title} {description} {text}".casefold()
    immigration = sorted({term for term in IMMIGRATION_TERMS if term in corpus})
    business = sorted({term for term in BUSINESS_TERMS if term in corpus})
    score = min(1.0, len(immigration) * 0.16 + len(business) * 0.12)
    return round(score, 2), immigration + business


def _ensure_source(db_path: str, seed_url: str) -> str:
    seed_domain = domain_of(seed_url) or "unknown"
    source_id = "discovery_" + hashlib.sha256(seed_domain.encode("utf-8")).hexdigest()[:12]
    with connect(db_path) as conn:
        conn.execute(
            """INSERT INTO sources(source_id,name,url,category,priority,access_method,status,notes)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET url=excluded.url,updated_at=CURRENT_TIMESTAMP""",
            (source_id, f"Public discovery seed: {seed_domain}", f"{urlparse(seed_url).scheme}://{urlparse(seed_url).netloc}/", "discovery", 10,
             "robots_checked_http", "enabled", "User-supplied public seed page; linked candidate sites are fetched only after robots checks."),
        )
    return source_id


def _extract_page(response: httpx.Response) -> tuple[str, str, str, BeautifulSoup]:
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    title = clean(soup.title.get_text(" ", strip=True) if soup.title else "") or ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = clean(meta.get("content", "") if meta else "") or ""
    text = " ".join(soup.get_text(" ", strip=True).split())[:20000]
    return title[:500], description[:1200], text, soup


def _is_excluded(domain: str) -> bool:
    return any(domain == item or domain.endswith("." + item) for item in EXCLUDED_DOMAINS)


def discover_from_seed(seed_url: str, db_path: str, *, max_candidates: int = 15, timeout: int = 12, delay: float = 1.0, min_score: float = 0.28, source_id: str | None = None) -> dict:
    """Inspect one public directory/source page and verify linked external organisation websites.

    This is deliberately a bounded one-seed discovery pass, not a general web search engine.
    """
    seed = validate_public_url(seed_url)
    source_id = source_id or _ensure_source(db_path, seed)
    with connect(db_path) as conn:
        if not conn.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
            raise RuntimeError(f"Fonte {source_id} não existe no Source Registry")
        run_id = conn.execute(
            "INSERT INTO discovery_runs(seed_url,source_id,run_type) VALUES(?,?,'html_seed_discovery')",
            (seed, source_id),
        ).lastrowid

    result = {"run_id": run_id, "seed_url": seed, "pages_checked": 0, "candidates_found": 0, "candidates_accepted": 0, "candidates_rejected": 0,
              "organisations_created": 0, "organisations_updated": 0, "candidates": [], "errors": []}
    robots_cache: dict[str, bool] = {}
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers, trust_env=False) as client:
            if not _robots_allows(client, seed, robots_cache):
                raise CrawlBlocked("robots.txt não autoriza a consulta da fonte inicial")
            response = bounded_get(client, seed, max_bytes=2_000_000, allowed_hosts={(urlparse(seed).hostname or "").lower()})
            if response.status_code != 200:
                raise CrawlBlocked(f"Fonte inicial respondeu HTTP {response.status_code}; redirects não são seguidos automaticamente")
            if "text/html" not in response.headers.get("content-type", "").lower():
                raise CrawlBlocked("A fonte inicial não devolveu HTML")
            _, _, _, soup = _extract_page(response)
            result["pages_checked"] += 1
            links: dict[str, tuple[str, str]] = {}
            seed_domain = domain_of(seed)
            for anchor in soup.find_all("a", href=True):
                raw = urldefrag(urljoin(seed, anchor["href"]))[0]
                try:
                    candidate_url = validate_public_url(raw)
                except (CrawlBlocked, ValueError):
                    continue
                domain = domain_of(candidate_url)
                if not domain or domain == seed_domain or _is_excluded(domain):
                    continue
                if candidate_url.lower().endswith((".pdf", ".xlsx", ".xls", ".doc", ".docx", ".zip")):
                    continue
                label = clean(anchor.get_text(" ", strip=True)) or ""
                if domain not in links or len(label) > len(links[domain][1]):
                    links[domain] = (candidate_url, label)
                if len(links) >= max_candidates:
                    break

            for domain, (candidate_url, anchor_text) in list(links.items()):
                try:
                    if not _robots_allows(client, candidate_url, robots_cache):
                        result["errors"].append({"domain": domain, "error": "robots.txt não autoriza"})
                        continue
                    page = bounded_get(client, candidate_url, max_bytes=2_000_000, allowed_hosts={(urlparse(candidate_url).hostname or "").lower()})
                    if page.status_code != 200 or "text/html" not in page.headers.get("content-type", "").lower():
                        result["errors"].append({"domain": domain, "error": f"homepage indisponível: HTTP {page.status_code}"})
                        continue
                    title, description, text, _ = _extract_page(page)
                    result["pages_checked"] += 1
                    score, terms = relevance(title, description, text)
                    accepted = score >= min_score and bool(terms)
                    name = title.split(" | ")[0].split(" - ")[0].strip() or domain
                    decision = "verified_candidate" if accepted else "rejected_low_relevance"
                    reason = "Sinais públicos de serviços de imigração: " + ", ".join(terms) if accepted else "Não foram encontrados sinais suficientes de serviços jurídicos/de imigração."
                    oid = None
                    if accepted:
                        with connect(db_path) as conn:
                            existing = conn.execute("SELECT organisation_id FROM organisations WHERE domain=? LIMIT 1", (domain,)).fetchone()
                            if existing:
                                oid = existing[0]
                                conn.execute("UPDATE organisations SET last_seen_at=CURRENT_TIMESTAMP WHERE organisation_id=?", (oid,))
                                result["organisations_updated"] += 1
                            else:
                                cur = conn.execute(
                                    """INSERT INTO organisations(canonical_name,normalized_name,website,domain,organisation_type,profile_status,confidence)
                                    VALUES(?,?,?,?,?,'discovered',?)""",
                                    (name, normalise_name(name), str(page.url), domain, "immigration_services_candidate", score),
                                )
                                oid = cur.lastrowid
                                result["organisations_created"] += 1
                            for field, value in (("website_title", title), ("website_description", description), ("immigration_service_signals", ", ".join(terms))):
                                if value:
                                    conn.execute(
                                    """INSERT INTO evidence(organisation_id,source_id,source_url,field_name,field_value,evidence_status,confidence,source_authority)
                                    VALUES(?,?,?,?,?,'discovery_observation',0.55,?)
                                    ON CONFLICT(organisation_id,source_id,source_url,field_name,field_value) DO UPDATE SET
                                    collected_at=CURRENT_TIMESTAMP,confidence=0.55,evidence_status='discovery_observation'""",
                                    (oid, source_id, str(page.url), field, value, source_id),
                                )
                    with connect(db_path) as conn:
                        conn.execute(
                            """INSERT INTO organisation_candidates(run_id,seed_url,candidate_url,domain,anchor_text,page_title,description,relevance_score,decision,decision_reason,organisation_id)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,domain) DO UPDATE SET candidate_url=excluded.candidate_url,
                            page_title=excluded.page_title,description=excluded.description,relevance_score=excluded.relevance_score,
                            decision=excluded.decision,decision_reason=excluded.decision_reason,organisation_id=excluded.organisation_id,checked_at=CURRENT_TIMESTAMP""",
                            (run_id, seed, candidate_url, domain, anchor_text, title, description, score, decision, reason, oid),
                        )
                    result["candidates"].append({"domain": domain, "name": name, "score": score, "decision": decision, "organisation_id": oid, "signals": terms})
                    result["candidates_found"] += 1
                    if accepted:
                        result["candidates_accepted"] += 1
                    else:
                        result["candidates_rejected"] += 1
                    if delay > 0:
                        time.sleep(delay)
                except (httpx.HTTPError, CrawlBlocked, ValueError) as exc:
                    result["errors"].append({"domain": domain, "error": str(exc)[:250]})
        with connect(db_path) as conn:
            conn.execute("UPDATE discovery_runs SET finished_at=CURRENT_TIMESTAMP,pages_checked=?,candidates_found=?,organisations_created=?,organisations_updated=?,status='completed' WHERE run_id=?",
                         (result["pages_checked"], result["candidates_found"], result["organisations_created"], result["organisations_updated"], run_id))
    except Exception as exc:
        result["errors"].append({"seed": seed, "error": str(exc)[:300]})
        with connect(db_path) as conn:
            conn.execute("UPDATE discovery_runs SET finished_at=CURRENT_TIMESTAMP,status='failed',error=? WHERE run_id=?", (str(exc)[:500], run_id))
    return result


def discover_all_sources(db_path: str, *, timeout: int = 12, delay: float = 1.0,
                          max_candidates_per_source: int = 15) -> dict:
    """Compatibility entry point; run the complete registry-driven SDI cycle."""
    from .engine import run_discovery_cycle
    return run_discovery_cycle(db_path, force=True)
