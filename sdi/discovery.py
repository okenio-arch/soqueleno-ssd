from __future__ import annotations

import json
import time
from urllib.parse import urljoin, urlparse, urldefrag

import httpx
from bs4 import BeautifulSoup

from .crawler import CrawlBlocked, USER_AGENT, ROBOT_TOKEN, _robots_allows, validate_public_url, bounded_get
from .db import connect
from .normalise import clean, domain_of, normalise_name, canonical_page_url

SOURCE_ID = "discovery_web"
IMMIGRATION_TERMS = (
    "immigration", "immigration law", "immigration solicitor", "immigration advice",
    "visa", "visas", "skilled worker", "family visa", "asylum", "ukvi",
    "indefinite leave to remain", "ilr", "settlement", "sponsor licence",
)
BUSINESS_TERMS = ("solicitor", "law firm", "legal services", "immigration adviser", "immigration advice", "lawyers", "legal practice", "regulated adviser")
SEED_NAV_TERMS = ("next", "previous", "page", "directory", "find", "search", "provider", "adviser", "solicitor", "immigration", "results", "view all")
EXCLUDED_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "youtube.com", "tiktok.com", "wikipedia.org", "google.com", "bing.com",
}


def relevance(title: str, description: str, text: str) -> tuple[float, list[str]]:
    """Score provider evidence, weighting page identity and service language over incidental mentions."""
    heading = f"{title} {description}".casefold()
    body = (text or "").casefold()
    immigration = sorted({term for term in IMMIGRATION_TERMS if term in body})
    business = sorted({term for term in BUSINESS_TERMS if term in body})
    heading_immigration = any(term in heading for term in IMMIGRATION_TERMS)
    heading_business = any(term in heading for term in BUSINESS_TERMS)
    if not immigration or not business:
        return 0.0, immigration + business
    score = min(0.55, len(immigration) * 0.07) + min(0.25, len(business) * 0.08)
    score += 0.12 if heading_immigration else 0.0
    score += 0.08 if heading_business else 0.0
    if len(body) < 350:
        score *= 0.65
    return round(min(score, 1.0), 2), immigration + business


def _extract_page(response: httpx.Response) -> tuple[str, str, str, BeautifulSoup]:
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    title = clean(soup.title.get_text(" ", strip=True) if soup.title else "") or ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = clean(meta.get("content", "") if meta else "") or ""
    text = " ".join(soup.get_text(" ", strip=True).split())[:20000]
    return title[:500], description[:1200], text, soup



def _blocked_source_reason(db_path: str, domain: str) -> str | None:
    """Do not fetch domains explicitly blocked or disabled in the source registry."""
    if not domain:
        return "Domínio inválido"
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name,url,status,source_type FROM sources WHERE status IN ('blocked_pending_authorisation','disabled','review_required')"
        ).fetchall()
    for row in rows:
        source_domain = domain_of(row["url"])
        if source_domain and (domain == source_domain or domain.endswith("." + source_domain)):
            return f"Domínio bloqueado pela política da fonte: {row['name']} ({row['status']})"
    return None


def _is_excluded(domain: str) -> bool:
    return any(domain == item or domain.endswith("." + item) for item in EXCLUDED_DOMAINS)


def discover_from_seed(seed_url: str, db_path: str, *, max_candidates: int = 15, max_seed_pages: int = 8, max_seed_depth: int = 2, timeout: int = 12, delay: float = 1.0, min_score: float = 0.28, source_id: str | None = None) -> dict:
    """Inspect one public directory/source page and verify linked external organisation websites.

    This is deliberately a bounded one-seed discovery pass, not a general web search engine.
    """
    seed = validate_public_url(seed_url)
    if not source_id:
        raise RuntimeError("A descoberta exige uma fonte-semente previamente registada e aprovada.")
    with connect(db_path) as conn:
        source_row = conn.execute(
            "SELECT status,source_type,config_json FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if not source_row:
            raise RuntimeError(f"Fonte {source_id} não existe no Source Registry")
        if source_row["status"] != "enabled" or source_row["source_type"] != "html_directory_seed":
            raise RuntimeError("A fonte não está aprovada como seed HTML ativa.")
        try:
            source_config = json.loads(source_row["config_json"] or "{}")
        except (TypeError, ValueError):
            source_config = {}
        if not (source_config.get("terms_reviewed") and source_config.get("crawl_allowed") and source_config.get("reuse_basis")):
            raise RuntimeError("A seed não tem revisão de termos e base de reutilização registadas.")
        run_id = conn.execute(
            "INSERT INTO discovery_runs(seed_url,source_id,run_type) VALUES(?,?,'html_seed_discovery')",
            (seed, source_id),
        ).lastrowid

    result = {"run_id": run_id, "seed_url": seed, "pages_checked": 0, "candidates_found": 0, "candidates_accepted": 0, "candidates_rejected": 0,
              "organisations_created": 0, "organisations_updated": 0, "seed_pages_remaining": 0, "candidates": [], "errors": []}
    robots_cache: dict[str, bool] = {}
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

    try:
        seed_host = (urlparse(seed).hostname or "").lower()
        seed_domain = domain_of(seed)
        page_budget = max(1, min(int(max_seed_pages), 30))
        depth_budget = max(0, min(int(max_seed_depth), 4))
        with connect(db_path) as conn:
            conn.execute(
                """UPDATE discovered_urls SET status='queued',
                last_error=coalesce(last_error,'Recovered after interrupted seed traversal')
                WHERE source_id=? AND discovery_type='seed_page' AND status='processing' AND attempts<3""",
                (source_id,),
            )
            pending_count = conn.execute(
                """SELECT count(*) FROM discovered_urls WHERE source_id=?
                AND discovery_type='seed_page' AND status IN ('queued','failed') AND attempts<3""",
                (source_id,),
            ).fetchone()[0]
            if pending_count == 0:
                conn.execute(
                    """INSERT INTO discovered_urls
                    (parent_url,discovered_url,domain,discovery_type,status,source_id,depth)
                    VALUES(?,?,?,'seed_page','queued',?,0)
                    ON CONFLICT(discovered_url) DO UPDATE SET
                    parent_url=excluded.parent_url,domain=excluded.domain,
                    discovery_type='seed_page',status='queued',source_id=excluded.source_id,
                    depth=0,attempts=0,last_error=NULL""",
                    (seed, seed, seed_domain, source_id),
                )
            conn.execute(
                """UPDATE discovered_urls SET status='queued'
                WHERE source_id=? AND discovery_type='seed_page' AND status='failed' AND attempts<3""",
                (source_id,),
            )

        links: dict[str, tuple[str, str]] = {}
        processed_seed_pages = 0
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers, trust_env=False) as client:
            while processed_seed_pages < page_budget and len(links) < max_candidates:
                with connect(db_path) as conn:
                    seed_item = conn.execute(
                        """SELECT discovered_url,depth FROM discovered_urls
                        WHERE source_id=? AND discovery_type='seed_page'
                        AND status='queued' AND attempts<3
                        ORDER BY depth,discovered_at LIMIT 1""",
                        (source_id,),
                    ).fetchone()
                if seed_item is None:
                    break
                seed_page = seed_item["discovered_url"]
                seed_depth = int(seed_item["depth"] or 0)
                processed_seed_pages += 1
                with connect(db_path) as conn:
                    conn.execute(
                        """UPDATE discovered_urls SET status='processing',attempts=attempts+1,
                        last_attempt_at=CURRENT_TIMESTAMP,last_error=NULL WHERE discovered_url=? AND source_id=?""",
                        (seed_page, source_id),
                    )
                try:
                    if not _robots_allows(client, seed_page, robots_cache):
                        raise CrawlBlocked("robots.txt não autoriza ou não pôde ser consultado")
                    response = bounded_get(client, seed_page, max_bytes=2_000_000, allowed_hosts={seed_host})
                    if response.status_code != 200 or "text/html" not in response.headers.get("content-type", "").lower():
                        raise CrawlBlocked(f"Página-semente não HTML ou HTTP {response.status_code}")
                    _, _, _, soup = _extract_page(response)
                    result["pages_checked"] += 1
                    with connect(db_path) as conn:
                        conn.execute(
                            "UPDATE discovered_urls SET status='processed',last_error=NULL WHERE discovered_url=? AND source_id=?",
                            (seed_page, source_id),
                        )
                    for anchor in soup.find_all("a", href=True):
                        raw = canonical_page_url(urldefrag(urljoin(str(response.url), anchor["href"]))[0])
                        label = clean(anchor.get_text(" ", strip=True)) or ""
                        try:
                            candidate_url = canonical_page_url(validate_public_url(raw))
                        except (CrawlBlocked, ValueError):
                            continue
                        domain = domain_of(candidate_url)
                        if not domain or candidate_url.lower().endswith((".pdf", ".xlsx", ".xls", ".doc", ".docx", ".zip")):
                            continue
                        if domain == seed_domain:
                            path_and_label = f"{urlparse(candidate_url).path} {urlparse(candidate_url).query} {label}".casefold()
                            is_directory_navigation = any(term in path_and_label for term in SEED_NAV_TERMS)
                            if seed_depth < depth_budget and is_directory_navigation:
                                with connect(db_path) as conn:
                                    conn.execute(
                                        """INSERT OR IGNORE INTO discovered_urls
                                        (parent_url,discovered_url,domain,discovery_type,status,anchor_text,source_id,depth)
                                        VALUES(?,?,?,'seed_page','queued',?,?,?)""",
                                        (seed_page, candidate_url, domain, label[:300], source_id, seed_depth + 1),
                                    )
                            continue
                        if _is_excluded(domain):
                            continue
                        if domain not in links or len(label) > len(links[domain][1]):
                            links[domain] = (candidate_url, label)
                        if len(links) >= max_candidates:
                            break
                    if delay > 0 and len(seed_pages) > 1:
                        time.sleep(delay)
                except (httpx.HTTPError, CrawlBlocked, ValueError) as exc:
                    result["errors"].append({"seed_page": seed_page, "error": str(exc)[:250]})
                    with connect(db_path) as conn:
                        conn.execute(
                            "UPDATE discovered_urls SET status='failed',last_error=? WHERE discovered_url=? AND source_id=?",
                            (str(exc)[:250], seed_page, source_id),
                        )
        with connect(db_path) as conn:
            result["seed_pages_remaining"] = conn.execute(
                """SELECT count(*) FROM discovered_urls WHERE source_id=? AND discovery_type='seed_page'
                AND status IN ('queued','failed') AND attempts<3""",
                (source_id,),
            ).fetchone()[0]

            for domain, (candidate_url, anchor_text) in list(links.items()):
                blocked_reason = _blocked_source_reason(db_path, domain)
                if blocked_reason:
                    with connect(db_path) as conn:
                        conn.execute(
                            """INSERT INTO organisation_candidates
                            (run_id,seed_url,candidate_url,domain,anchor_text,relevance_score,decision,decision_reason)
                            VALUES(?,?,?,?,?,0,'rejected_source_policy',?)
                            ON CONFLICT(run_id,domain) DO UPDATE SET
                            decision='rejected_source_policy',decision_reason=excluded.decision_reason,
                            checked_at=CURRENT_TIMESTAMP""",
                            (run_id, seed, candidate_url, domain, anchor_text, blocked_reason),
                        )
                    with connect(db_path) as conn:
                        conn.execute(
                            """INSERT OR IGNORE INTO discovered_urls
                            (parent_url,discovered_url,domain,discovery_type,status,anchor_text,source_id,depth)
                            VALUES(?,?,?,'seed_candidate','rejected_source_policy',?,?,1)""",
                            (seed, candidate_url, domain, anchor_text[:300], source_id),
                        )
                    result["candidates_found"] += 1
                    result["candidates_rejected"] += 1
                    result["candidates"].append({
                        "domain": domain, "score": 0.0,
                        "decision": "rejected_source_policy",
                        "reason": blocked_reason,
                    })
                    continue
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
                    with connect(db_path) as conn:
                        conn.execute(
                            """INSERT OR IGNORE INTO discovered_urls
                            (parent_url,discovered_url,domain,discovery_type,status,anchor_text,source_id,depth)
                            VALUES(?,?,?,'seed_candidate','candidate_only',?,?,1)""",
                            (seed, candidate_url, domain, anchor_text[:300], source_id),
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
