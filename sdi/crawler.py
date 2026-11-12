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
from .normalise import normalise_url, domain_of, clean, canonical_page_url

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


def _resolve_public_addresses(host: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CrawlBlocked("Host não resolvido") from exc
    addresses = set()
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError as exc:
            raise CrawlBlocked("DNS devolveu um endereço IP inválido") from exc
        if not ip.is_global:
            raise CrawlBlocked("Host resolve para um endereço IP não público")
        addresses.add(ip)
    if not addresses:
        raise CrawlBlocked("Host sem endereços públicos")
    return addresses


def _public_host(host: str) -> bool:
    try:
        _resolve_public_addresses(host)
        return True
    except CrawlBlocked:
        return False


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


def bounded_get(client: httpx.Client, url: str, *, max_bytes: int = 2_000_000,
                allowed_hosts: set[str] | None = None, max_redirects: int = 3) -> httpx.Response:
    """GET a public URL with streamed byte limits and explicit redirect validation."""
    current = validate_public_url(url)
    for hop in range(max(0, min(int(max_redirects), 5)) + 1):
        parsed = urlparse(current)
        host = (parsed.hostname or "").lower()
        if allowed_hosts is not None and host not in allowed_hosts:
            raise CrawlBlocked("Host fora da allowlist da fonte")
        # Pin the socket destination to a validated public IP. Host and TLS SNI
        # remain the original hostname, so virtual hosting and certificate checks work.
        current = validate_public_url(current)
        expected_addresses = sorted(_resolve_public_addresses(host), key=str)
        response = None
        request = None
        last_connect_error = None
        for pinned_ip in expected_addresses:
            ip_host = f"[{pinned_ip}]" if pinned_ip.version == 6 else str(pinned_ip)
            port_suffix = f":{parsed.port}" if parsed.port is not None else ""
            connect_url = current.replace(parsed.netloc, ip_host + port_suffix, 1)
            candidate_request = client.build_request("GET", connect_url)
            host_header = f"[{host}]" if ":" in host else host
            candidate_request.headers["Host"] = host_header + port_suffix
            if parsed.scheme == "https":
                candidate_request.extensions["sni_hostname"] = host
            try:
                response = client.send(candidate_request, stream=True, follow_redirects=False)
                request = candidate_request
                request.url = httpx.URL(current)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_connect_error = exc
        if response is None or request is None:
            if last_connect_error:
                raise last_connect_error
            raise CrawlBlocked("Não foi possível estabelecer ligação ao IP público validado")
        try:
            peer = response.extensions.get("network_stream")
            if peer is None:
                raise CrawlBlocked("Não foi possível verificar o endereço IP remoto da ligação")
            address = peer.get_extra_info("server_addr")
            if not address:
                raise CrawlBlocked("A ligação não revelou o endereço IP remoto")
            try:
                peer_ip = ipaddress.ip_address(address[0])
            except ValueError as exc:
                raise CrawlBlocked("Endereço IP remoto inválido") from exc
            if not peer_ip.is_global or peer_ip != pinned_ip:
                raise CrawlBlocked("O IP remoto não corresponde ao destino DNS fixado")
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location or hop >= max_redirects:
                    raise CrawlBlocked("Redirecionamento ausente ou limite de redirecionamentos atingido")
                target = validate_public_url(urljoin(current, location))
                target_host = (urlparse(target).hostname or "").lower()
                if allowed_hosts is not None and target_host not in allowed_hosts:
                    raise CrawlBlocked("Redirecionamento para host não autorizado")
                current = target
                continue
            length = response.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise CrawlBlocked(f"Resposta excede o limite de {max_bytes} bytes")
            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise CrawlBlocked(f"Resposta excede o limite de {max_bytes} bytes")
                chunks.append(chunk)
            return httpx.Response(
                response.status_code, headers=response.headers, content=b"".join(chunks),
                request=request, extensions=response.extensions,
            )
        finally:
            response.close()
    raise CrawlBlocked("Limite de redirecionamentos atingido")


def _robots_parser(client: httpx.Client, url: str, cache: dict[str, RobotFileParser | None]) -> RobotFileParser | None:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}".lower()
    if origin in cache:
        return cache[origin]
    robots_url = origin + "/robots.txt"
    try:
        response = bounded_get(client, robots_url, max_bytes=512_000)
        if response.status_code in (401, 403, 429) or response.status_code >= 500 or 300 <= response.status_code < 400:
            cache[origin] = None
            return None
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines() if response.status_code < 400 else [])
        cache[origin] = parser
        return parser
    except (httpx.HTTPError, CrawlBlocked):
        cache[origin] = None
        return None


def _robots_allows(client: httpx.Client, url: str, cache: dict) -> bool:
    parser = _robots_parser(client, url, cache)
    if parser is None:
        return False
    return parser.can_fetch(ROBOT_TOKEN, url)


def _sitemaps(client: httpx.Client, origin: str, robots_cache: dict, max_urls: int) -> list[str]:
    parser = _robots_parser(client, origin + "/", robots_cache)
    pending = list(parser.site_maps() if parser else [])
    seen_sitemaps: set[str] = set()
    page_urls: list[str] = []
    origin_domain = domain_of(origin)
    origin_host = (urlparse(origin).hostname or "").lower()
    while pending and len(seen_sitemaps) < 5 and len(page_urls) < max_urls:
        sitemap = pending.pop(0)
        try:
            sitemap = canonical_page_url(validate_public_url(sitemap))
            if sitemap in seen_sitemaps or domain_of(sitemap) != origin_domain:
                continue
            seen_sitemaps.add(sitemap)
            if not _robots_allows(client, sitemap, robots_cache):
                continue
            response = bounded_get(client, sitemap, max_bytes=2_000_000, allowed_hosts={origin_host})
            if response.status_code != 200:
                continue
            root = ET.fromstring(response.content)
            is_index = root.tag.lower().endswith("sitemapindex")
            for node in root.iter():
                if not node.tag.lower().endswith("loc") or not node.text:
                    continue
                try:
                    candidate = canonical_page_url(validate_public_url(node.text.strip()))
                except CrawlBlocked:
                    continue
                if domain_of(candidate) != origin_domain:
                    continue
                if is_index:
                    if candidate not in seen_sitemaps and candidate not in pending:
                        pending.append(candidate)
                elif candidate not in page_urls:
                    page_urls.append(candidate)
                    if len(page_urls) >= max_urls:
                        break
        except (httpx.HTTPError, ET.ParseError, CrawlBlocked, ValueError):
            continue
    return page_urls


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


def _indexable_page(url: str, title: str, description: str, text: str) -> bool:
    """Reject navigation, account, archive and thin pages before search indexing."""
    path = (urlparse(url).path or "/").casefold()
    blocked_segments = (
        "/wp-admin", "/wp-login", "/login", "/logout", "/register",
        "/search", "/tag/", "/category/", "/author/", "/feed",
        "/privacy", "/cookie", "/terms", "/accessibility",
    )
    if any(segment in path for segment in blocked_segments):
        return False
    if re.search(r"/(page|tag|category)/\\d+/?$", path):
        return False
    body = clean(" ".join((title or "", description or "", text or ""))) or ""
    return len(body) >= 220


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
            """INSERT INTO sources(source_id,name,url,category,priority,access_method,status,notes,source_type,config_json)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                url=excluded.url,
                name=excluded.name,
                notes=excluded.notes,
                source_type=excluded.source_type,
                updated_at=CURRENT_TIMESTAMP""",
            (SOURCE_ID, "Imported organisation websites", "sdi://organisation-websites", "organisation_websites", 20,
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
    queue = deque([(canonical_page_url(start), 0)])
    seen: set[str] = set()
    pages = external_candidates = 0
    indexed_pages = excluded_pages = 0
    errors: list[dict] = []
    robots_cache: dict = {}
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers, trust_env=False) as client:
        # The sitemap is an index of the same public website, not a search-engine API.
        for sitemap_url in _sitemaps(client, f"{urlparse(start).scheme}://{urlparse(start).netloc}", robots_cache, max_pages):
            queue.append((sitemap_url, 1))
        while queue and pages < max(1, min(max_pages, 100)):
            url, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                url = canonical_page_url(validate_public_url(url))
                if domain_of(url) != host:
                    continue
                if not _robots_allows(client, url, robots_cache):
                    errors.append({"url": url, "error": "robots.txt não autoriza ou não pôde ser consultado"})
                    continue
                response = bounded_get(client, url, max_bytes=2_000_000, allowed_hosts={(urlparse(start).hostname or "").lower()})
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
                indexable = not noindex and "noindex" not in response.headers.get("x-robots-tag", "").casefold()
                indexable = indexable and _indexable_page(url, title, description, text)
                with connect(db_path) as conn:
                    previous = conn.execute(
                        "SELECT page_id,content_hash FROM crawled_pages WHERE url=?", (url,)
                    ).fetchone()
                    duplicate = conn.execute(
                        """SELECT url FROM crawled_pages WHERE organisation_id=? AND content_hash=?
                        AND url<>? AND is_indexable=1 ORDER BY fetched_at ASC LIMIT 1""",
                        (organisation_id, digest, url),
                    ).fetchone() if indexable else None
                    canonical_url = duplicate["url"] if duplicate else None
                    conn.execute(
                        """INSERT INTO crawled_pages(organisation_id,source_id,url,final_url,http_status,title,description,text_content,content_hash,crawl_status,depth,is_indexable,duplicate_of_url)
                        VALUES(?,?,?,?,?,?,?,?,?,'success',?,?,?)
                        ON CONFLICT(url) DO UPDATE SET organisation_id=excluded.organisation_id,final_url=excluded.final_url,http_status=excluded.http_status,
                        title=excluded.title,description=excluded.description,text_content=excluded.text_content,content_hash=excluded.content_hash,
                        fetched_at=CURRENT_TIMESTAMP,crawl_status='success',error=NULL,depth=excluded.depth,is_indexable=excluded.is_indexable,
                        duplicate_of_url=excluded.duplicate_of_url""",
                        (organisation_id, SOURCE_ID, url, final_url, response.status_code, title, description, text, digest, depth,
                         0 if canonical_url or not indexable else 1, canonical_url),
                    )
                    page_row = conn.execute("SELECT page_id FROM crawled_pages WHERE url=?", (url,)).fetchone()
                    page_id = int(page_row["page_id"])
                    if not previous or previous["content_hash"] != digest:
                        change_type = "new" if not previous else "changed"
                        conn.execute(
                            """INSERT OR IGNORE INTO page_versions
                            (page_id,organisation_id,source_id,url,content_hash,title,description,text_content,change_type)
                            VALUES(?,?,?,?,?,?,?,?,?)""",
                            (page_id, organisation_id, SOURCE_ID, url, digest, title, description, text, change_type),
                        )
                    if canonical_url or not indexable:
                        pages += 1
                        excluded_pages += 1
                        continue
                    fields = {"website_page_title": title, "website_page_description": description}
                    signals = _service_signals(f"{title} {description} {text}")
                    if signals:
                        fields["immigration_service_signals"] = ", ".join(signals)
                    for field, value in fields.items():
                        if value:
                            conn.execute(
                                """INSERT INTO evidence(organisation_id,source_id,source_url,field_name,field_value,evidence_status,confidence,source_authority)
                                VALUES(?,?,?,?,?,'website_observation',0.60,'Organisation website')
                                ON CONFLICT(organisation_id,source_id,source_url,field_name,field_value) DO UPDATE SET
                                collected_at=CURRENT_TIMESTAMP,confidence=0.60,evidence_status='website_observation'""",
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
                        conn.execute("""INSERT INTO evidence(organisation_id,source_id,source_url,field_name,field_value,evidence_status,confidence,source_authority)
                            VALUES(?,?,?,?,?,'website_observation',0.60,'Organisation website')
                            ON CONFLICT(organisation_id,source_id,source_url,field_name,field_value) DO UPDATE SET collected_at=CURRENT_TIMESTAMP""",
                            (organisation_id, SOURCE_ID, url, "public_email", email))
                    if phone:
                        conn.execute("""INSERT INTO evidence(organisation_id,source_id,source_url,field_name,field_value,evidence_status,confidence,source_authority)
                            VALUES(?,?,?,?,?,'website_observation',0.60,'Organisation website')
                            ON CONFLICT(organisation_id,source_id,source_url,field_name,field_value) DO UPDATE SET collected_at=CURRENT_TIMESTAMP""",
                            (organisation_id, SOURCE_ID, url, "public_phone", phone))
                    for anchor in soup.find_all("a", href=True):
                        raw = urldefrag(urljoin(final_url, anchor["href"]))[0]
                        label = clean(anchor.get_text(" ", strip=True)) or ""
                        try:
                            candidate = canonical_page_url(validate_public_url(raw))
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
                indexed_pages += 1
                if delay > 0:
                    time.sleep(delay)
            except (httpx.HTTPError, CrawlBlocked, ValueError) as exc:
                errors.append({"url": url, "error": str(exc)[:250]})
    final_status = "completed" if pages and not errors else ("completed_with_errors" if pages else "blocked_or_failed")
    with connect(db_path) as conn:
        conn.execute("UPDATE organisations SET website_crawl_status=?,website_last_crawled_at=CASE WHEN ? > 0 THEN CURRENT_TIMESTAMP ELSE website_last_crawled_at END WHERE organisation_id=?",
                     (final_status, pages, organisation_id))
    return {"organisation_id": organisation_id, "start_url": start, "pages_crawled": pages,
            "unique_urls_seen": len(seen), "pages_indexed": indexed_pages,
            "pages_excluded": excluded_pages, "external_candidates_recorded": external_candidates,
            "crawl_status": final_status, "errors": errors}
