from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .db import connect
from .normalise import normalise_url, domain_of, clean

SOURCE_ID = "organisation_websites"
USER_AGENT = "SoquelenoSDI/0.8 (local UK immigration provider research index)"
ROBOT_TOKEN = "SoquelenoSDI"
GENERIC_EMAIL_PREFIXES = ("info", "contact", "enquiries", "enquiry", "office", "admin", "hello", "reception", "legal", "immigration", "support")
SERVICE_TERMS = (
    "immigration", "visa", "skilled worker", "global talent", "family visa", "spouse visa",
    "student visa", "asylum", "human rights", "settlement", "indefinite leave to remain",
    "ilr", "naturalisation", "citizenship", "sponsor licence", "business immigration",
    "deportation", "appeal", "judicial review", "ukvi",
)

class CrawlBlocked(ValueError):
    pass


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    addresses = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
        addresses.append(ip)
    return bool(addresses)


def validate_public_url(url: str) -> str:
    value = normalise_url(url)
    if not value:
        raise CrawlBlocked("URL inválido")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise CrawlBlocked("Apenas HTTP e HTTPS são permitidos")
    if parsed.username or parsed.password:
        raise CrawlBlocked("URLs com credenciais não são permitidos")
    if parsed.port not in (None, 80, 443):
        raise CrawlBlocked("Portas não padrão bloqueadas")
    if not _public_host(parsed.hostname or ""):
        raise CrawlBlocked("Host não público ou não resolvido")
    return value


def _robots_parser(client: httpx.Client, url: str, cache: dict[str, RobotFileParser | None]) -> RobotFileParser | None:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}".lower()
    if origin in cache:
        return cache[origin]
    robots_url = origin + "/robots.txt"
    try:
        response = client.get(robots_url, headers={"User-Agent": USER_AGENT})
        if response.status_code in (401, 403, 429) or response.status_code >= 500 or 300 <= response.status_code < 400:
            cache[origin] = None
            return None
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines() if response.status_code < 400 else [])
        cache[origin] = parser
        return parser
    except httpx.HTTPError:
        cache[origin] = None
        return None


def _robots_allows(client: httpx.Client, url: str, cache: dict) -> bool:
    parser = _robots_parser(client, url, cache)
    if parser is None:
        return False
    return parser.can_fetch(ROBOT_TOKEN, url)


def _sitemaps(client: httpx.Client, origin: str, robots_cache: dict, max_urls: int) -> list[str]:
    parser = _robots_parser(client, origin + "/", robots_cache)
    sitemap_urls = []
    # RobotFileParser does not expose Sitemap directives; parse robots.txt separately.
    try:
        resp = client.get(origin.rstrip("/") + "/robots.txt", headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_urls.append(line.split(":", 1)[1].strip())
    except httpx.HTTPError:
        return []
    urls: list[str] = []
    for sitemap in sitemap_urls[:3]:
        try:
            sitemap = validate_public_url(sitemap)
            if not _robots_allows(client, sitemap, robots_cache):
                continue
            response = client.get(sitemap)
            if response.status_code != 200:
                continue
            root = ET.fromstring(response.content)
            for node in root.iter():
                if node.tag.endswith("loc") and node.text:
                    candidate = node.text.strip()
                    try:
                        candidate = validate_public_url(candidate)
                    except CrawlBlocked:
                        continue
                    if domain_of(candidate) == domain_of(origin):
                        urls.append(candidate)
                        if len(urls) >= max_urls:
                            return urls
        except (httpx.HTTPError, ET.ParseError, CrawlBlocked, ValueError):
            continue
    return urls


def _parse_page(response: httpx.Response) -> tuple[str, str, str, BeautifulSoup, bool]:
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "canvas"]):
        tag.decompose()
    title = clean(soup.title.get_text(" ", strip=True) if soup.title else "") or ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = clean(meta.get("content", "") if meta else "") or ""
    robots = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    noindex = bool(robots and "noindex" in (robots.get("content", "").casefold()))
    text = " ".join(soup.get_text(" ", strip=True).split())[:12000]
    return title[:500], description[:1200], text, soup, noindex


def _generic_email(text: str) -> str | None:
    for email in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text or "", re.I):
        local = email.split("@", 1)[0].casefold()
        if any(local == prefix or local.startswith(prefix + ".") or local.startswith(prefix + "-") for prefix in GENERIC_EMAIL_PREFIXES):
            return email.lower()
    return None


def _phone(text: str) -> str | None:
    for value in re.findall(r"(?:\+44|0)[\d\s().-]{8,18}\d", text or ""):
        compact = re.sub(r"\D", "", value)
        if 10 <= len(compact) <= 13:
            return clean(value)
    return None


def _service_signals(text: str) -> list[str]:
    low = (text or "").casefold()
    return [term for term in SERVICE_TERMS if term in low]


def _ensure_website_source(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO sources(source_id,name,url,category,priority,access_method,status,notes,source_type,config_json)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (SOURCE_ID, "Imported organisation websites", "https://example.invalid", "organisation_websites", 20,
             "robots_checked_http", "enabled", "Limited crawl of websites listed by official datasets or approved sources; same-site pages only.",
             "website_crawl", "{}"),
        )


def crawl_organisation(organisation_id: int, start_url: str, db_path: str, *, max_pages: int = 12,
                      max_depth: int = 2, timeout: int = 12, delay: float = 1.5) -> dict:
    start = validate_public_url(start_url)
    host = domain_of(start)
    _ensure_website_source(db_path)
    with connect(db_path) as conn:
        conn.execute("UPDATE organisations SET website_last_attempt_at=CURRENT_TIMESTAMP,website_crawl_status='running' WHERE organisation_id=?", (organisation_id,))
    queue = deque([(start, 0)])
    seen: set[str] = set()
    pages = external_candidates = 0
    errors: list[dict] = []
    robots_cache: dict = {}
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
        # The sitemap is an index of the same public website, not a search-engine API.
        for sitemap_url in _sitemaps(client, f"{urlparse(start).scheme}://{urlparse(start).netloc}", robots_cache, max_pages):
            queue.append((sitemap_url, 1))
        while queue and pages < max(1, min(max_pages, 100)):
            url, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                url = validate_public_url(url)
                if domain_of(url) != host:
                    continue
                if not _robots_allows(client, url, robots_cache):
                    errors.append({"url": url, "error": "robots.txt não autoriza ou não pôde ser consultado"})
                    continue
                response = client.get(url)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if location:
                        target = validate_public_url(urljoin(url, location))
                        if domain_of(target) == host and _robots_allows(client, target, robots_cache):
                            queue.appendleft((target, depth))
                    continue
                if response.status_code != 200:
                    errors.append({"url": url, "error": f"HTTP {response.status_code}"})
                    continue
                if "text/html" not in response.headers.get("content-type", "").lower():
                    continue
                title, description, text, soup, noindex = _parse_page(response)
                digest = hashlib.sha256((title + "\n" + description + "\n" + text).encode("utf-8")).hexdigest()
                final_url = str(response.url)
                if noindex or "noindex" in response.headers.get("x-robots-tag", "").casefold():
                    with connect(db_path) as conn:
                        conn.execute("UPDATE crawled_pages SET is_indexable=0,fetched_at=CURRENT_TIMESTAMP WHERE url=?", (url,))
                    continue
                with connect(db_path) as conn:
                    conn.execute(
                        """INSERT INTO crawled_pages(organisation_id,source_id,url,final_url,http_status,title,description,text_content,content_hash,crawl_status,depth,is_indexable)
                        VALUES(?,?,?,?,?,?,?,?,?,'success',?,1)
                        ON CONFLICT(url) DO UPDATE SET organisation_id=excluded.organisation_id,final_url=excluded.final_url,http_status=excluded.http_status,
                        title=excluded.title,description=excluded.description,text_content=excluded.text_content,content_hash=excluded.content_hash,
                        fetched_at=CURRENT_TIMESTAMP,crawl_status='success',error=NULL,depth=excluded.depth,is_indexable=1""",
                        (organisation_id, SOURCE_ID, url, final_url, response.status_code, title, description, text, digest, depth),
                    )
                    fields = {"website_page_title": title, "website_page_description": description}
                    signals = _service_signals(f"{title} {description} {text}")
                    if signals:
                        fields["immigration_service_signals"] = ", ".join(signals)
                    for field, value in fields.items():
                        if value:
                            conn.execute(
                                "INSERT OR IGNORE INTO evidence(organisation_id,source_id,source_url,field_name,field_value,evidence_status) VALUES(?,?,?,?,?,'website_observation')",
                                (organisation_id, SOURCE_ID, url, field, value),
                            )
                    org = conn.execute("SELECT immigration_services,phone,public_email FROM organisations WHERE organisation_id=?", (organisation_id,)).fetchone()
                    merged = sorted(set((org[0] or "").split("; ")) | set(signals)) if signals else []
                    email = _generic_email(" ".join([text] + [a.get("href", "")[7:] for a in soup.find_all("a", href=True) if a.get("href", "").lower().startswith("mailto:")]))
                    phone = org[1] or _phone(text)
                    conn.execute(
                        "UPDATE organisations SET immigration_services=CASE WHEN ?='' THEN immigration_services ELSE ? END,public_email=coalesce(public_email,?),phone=coalesce(phone,?),website_last_crawled_at=CURRENT_TIMESTAMP,profile_status='enriched',last_seen_at=CURRENT_TIMESTAMP,source_quality=max(source_quality,0.82) WHERE organisation_id=?",
                        ("; ".join(merged), "; ".join(merged), email, phone, organisation_id),
                    )
                    if email:
                        conn.execute("INSERT OR IGNORE INTO evidence(organisation_id,source_id,source_url,field_name,field_value,evidence_status) VALUES(?,?,?,?,?,'website_observation')", (organisation_id, SOURCE_ID, url, "public_email", email))
                    if phone:
                        conn.execute("INSERT OR IGNORE INTO evidence(organisation_id,source_id,source_url,field_name,field_value,evidence_status) VALUES(?,?,?,?,?,'website_observation')", (organisation_id, SOURCE_ID, url, "public_phone", phone))
                    for anchor in soup.find_all("a", href=True):
                        raw = urldefrag(urljoin(final_url, anchor["href"]))[0]
                        label = clean(anchor.get_text(" ", strip=True)) or ""
                        try:
                            candidate = validate_public_url(raw)
                        except CrawlBlocked:
                            continue
                        candidate_domain = domain_of(candidate)
                        if not candidate_domain:
                            continue
                        if candidate_domain == host and depth < max_depth and candidate not in seen:
                            queue.append((candidate, depth + 1))
                        elif candidate_domain != host and not any(candidate_domain == d or candidate_domain.endswith("." + d) for d in (
                            "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "youtube.com", "tiktok.com")):
                            cur = conn.execute(
                                "INSERT OR IGNORE INTO discovered_urls(parent_url,discovered_url,domain,discovery_type,status,anchor_text) VALUES(?,?,?,?,?,?)",
                                (url, candidate, candidate_domain, "external_reference", "candidate_only", label[:300]),
                            )
                            if cur.rowcount > 0:
                                external_candidates += 1
                pages += 1
                if delay > 0:
                    time.sleep(delay)
            except (httpx.HTTPError, CrawlBlocked, ValueError) as exc:
                errors.append({"url": url, "error": str(exc)[:250]})
    final_status = "completed" if pages and not errors else ("completed_with_errors" if pages else "blocked_or_failed")
    with connect(db_path) as conn:
        conn.execute("UPDATE organisations SET website_crawl_status=?,website_last_crawled_at=CASE WHEN ? > 0 THEN CURRENT_TIMESTAMP ELSE website_last_crawled_at END WHERE organisation_id=?",
                     (final_status, pages, organisation_id))
    return {"organisation_id": organisation_id, "start_url": start, "pages_crawled": pages,
            "unique_urls_seen": len(seen), "external_candidates_recorded": external_candidates,
            "crawl_status": final_status, "errors": errors}
