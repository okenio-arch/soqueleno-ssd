from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .db import connect
from .crawler import USER_AGENT, _robots_allows, validate_public_url, CrawlBlocked
from .normalise import clean, normalise_name, normalise_url, domain_of
from .iaa_import import import_iaa_xlsx

ALIASES = {
    "name": {"organisation name", "organization name", "name of organisation", "name of organization", "registered name", "provider name", "provider organisation", "provider organization", "provider s name", "provider", "firm name", "supplier name", "legal aid provider", "organisation", "organization", "name"},
    "reference": {"provider number", "provider id", "provider reference", "provider office id", "contract number", "contract reference", "office id", "supplier number", "legal aid contract number", "registration number"},
    "website": {"website", "web site", "website address", "web address", "internet address"},
    "email": {"email", "email address", "e mail", "public email", "office email"},
    "phone": {"telephone", "telephone number", "phone", "phone number", "telephone no", "contact telephone"},
    "address1": {"address", "office address", "provider address", "address line 1", "address 1", "street"},
    "address2": {"address line 2", "address 2"},
    "city": {"town", "city", "town city", "locality"},
    "region": {"county", "region", "area", "local authority"},
    "postcode": {"postcode", "post code", "postal code"},
    "category": {"category", "category of law", "area of law", "areas of law", "main area of law", "matter category", "matter type", "law type", "legal aid category", "contract category", "contract type", "service area", "specialism"},
}


def _norm(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _date_from_page(text: str, fallback: str | None) -> str | None:
    m = re.search(r"(?:last updated|published)\s*:?\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text, re.I)
    if not m:
        return fallback
    try:
        return datetime.strptime(m.group(1), "%d %B %Y").date().isoformat()
    except ValueError:
        return fallback


def download_govuk_xlsx(source: dict, db_path: str, *, timeout: int = 20, max_bytes: int = 35_000_000) -> dict:
    """Find the current XLSX on a GOV.UK publication page and download it locally.

    Only GOV.UK publication pages and assets.publishing.service.gov.uk files are accepted.
    """
    page_url = source["url"]
    cfg = source.get("config", {})
    page_host = (urlparse(page_url).hostname or "").lower()
    if page_host not in {"gov.uk", "www.gov.uk"}:
        raise ValueError("O coletor GOV.UK só aceita páginas oficiais gov.uk")
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    robots_cache: dict[str, bool] = {}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        if not _robots_allows(client, page_url, robots_cache):
            raise CrawlBlocked("robots.txt não autoriza a consulta da publicação GOV.UK")
        page = client.get(page_url)
        if page.status_code != 200 or "text/html" not in page.headers.get("content-type", "").lower():
            raise RuntimeError(f"Publicação GOV.UK indisponível: HTTP {page.status_code}")
        soup = BeautifulSoup(page.text, "html.parser")
        expected = _norm(cfg.get("download_link_text", ""))
        matches = []
        for a in soup.find_all("a", href=True):
            href = urljoin(str(page.url), a["href"])
            label = _norm(a.get_text(" ", strip=True))
            host = (urlparse(href).hostname or "").lower()
            if host != "assets.publishing.service.gov.uk" or not href.lower().split("?", 1)[0].endswith(".xlsx"):
                continue
            if expected and expected in label:
                matches.append((href, label))
        if not matches:
            raise RuntimeError("Não encontrei o XLSX oficial esperado na página GOV.UK; o layout/link pode ter mudado")
        asset_url = matches[0][0]
        if not _robots_allows(client, asset_url, robots_cache):
            raise CrawlBlocked("robots.txt do host de ficheiros não autoriza o download")
        asset = client.get(asset_url)
        if asset.status_code != 200:
            raise RuntimeError(f"Download do dataset falhou: HTTP {asset.status_code}")
        data = asset.content
        if len(data) > max_bytes:
            raise RuntimeError(f"Dataset excede o limite de {max_bytes} bytes")
        if not data.startswith(b"PK"):
            raise RuntimeError("O documento obtido não parece ser um ficheiro XLSX válido")
        published = _date_from_page(BeautifulSoup(page.text, "html.parser").get_text(" ", strip=True), cfg.get("published_at"))
    digest = hashlib.sha256(data).hexdigest()
    storage_dir = Path(db_path).resolve().parent.parent / "storage" / "datasets"
    storage_dir.mkdir(parents=True, exist_ok=True)
    target = storage_dir / f"{source['source_id']}_{digest[:12]}.xlsx"
    if not target.exists():
        target.write_bytes(data)
    return {"file": str(target), "filename": target.name, "asset_url": asset_url, "sha256": digest,
            "bytes": len(data), "published_at": published, "downloaded_at": datetime.now(timezone.utc).isoformat()}


def _header_map(values) -> dict[str, int]:
    found: dict[str, int] = {}
    for i, value in enumerate(values):
        key = _norm(value)
        for field, aliases in ALIASES.items():
            if key in aliases and field not in found:
                found[field] = i
    return found


def _cell(row, cols: dict[str, int], field: str):
    idx = cols.get(field)
    if idx is None or idx >= len(row):
        return None
    return clean(row[idx])


def _merge_categories(existing: str | None, incoming: str | None) -> str | None:
    """Merge semicolon-delimited categories without accumulating duplicates."""
    values: list[str] = []
    seen: set[str] = set()
    for raw in (existing or "", incoming or ""):
        for item in raw.split(";"):
            value = clean(item)
            key = value.casefold() if value else ""
            if value and key not in seen:
                seen.add(key)
                values.append(value)
    return "; ".join(values) or None


def _evidence(conn, oid: int, source_id: str, source_url: str, field: str, value: str | None,
              published_at: str | None, dataset_as_of: str | None, status: str = "official_snapshot"):
    if value:
        conn.execute(
            """INSERT OR IGNORE INTO evidence(organisation_id,source_id,source_url,field_name,field_value,source_published_at,dataset_as_of,evidence_status)
            VALUES(?,?,?,?,?,?,?,?)""",
            (oid, source_id, source_url, field, value, published_at, dataset_as_of, status),
        )


def import_provider_workbook(xlsx_path: str, db_path: str, source: dict, *, dry_run: bool = False,
                             published_at: str | None = None, dataset_as_of: str | None = None) -> dict:
    from openpyxl import load_workbook
    path = Path(xlsx_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    cfg = source.get("config", {})
    source_id = source["source_id"]
    source_url = source["url"]
    filters = [_norm(x) for x in cfg.get("category_filter", ["immigration", "asylum"])]
    published_at = published_at or cfg.get("published_at")
    dataset_as_of = dataset_as_of or cfg.get("dataset_as_of")
    wb = load_workbook(path, read_only=True, data_only=True)
    parsed: list[dict] = []
    rows_seen = skipped = sheets_used = 0
    header_details = []
    try:
        for ws in wb.worksheets:
            found = None
            for row_no, values in enumerate(ws.iter_rows(min_row=1, max_row=35, values_only=True), 1):
                cols = _header_map(values)
                if "name" in cols and len(cols) >= 2:
                    found = (row_no, cols)
                    break
            if not found:
                continue
            header_row, cols = found
            sheets_used += 1
            sheet_text = _norm(ws.title)
            header_details.append({"sheet": ws.title, "header_row": header_row, "columns": sorted(cols)})
            for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
                if not any(v is not None and str(v).strip() for v in row):
                    continue
                rows_seen += 1
                category = _cell(row, cols, "category")
                # A global legal-aid directory contains many non-immigration practices.
                # Only ingest a row if its category or worksheet establishes relevance.
                category_text = _norm(category or "")
                sheet_relevant = any(term in sheet_text for term in filters)
                row_relevant = any(term in category_text for term in filters)
                if filters and "category" in cols and not row_relevant:
                    skipped += 1
                    continue
                if filters and "category" not in cols and not sheet_relevant and not cfg.get("source_scope"):
                    skipped += 1
                    continue
                name = _cell(row, cols, "name")
                if not name:
                    skipped += 1
                    continue
                website = normalise_url(_cell(row, cols, "website"))
                address_parts = [_cell(row, cols, f) for f in ("address1", "address2", "city", "region", "postcode")]
                address = ", ".join(x for x in address_parts if x)
                parsed.append({
                    "name": name, "reference": _cell(row, cols, "reference"), "website": website,
                    "domain": domain_of(website), "email": _cell(row, cols, "email"), "phone": _cell(row, cols, "phone"),
                    "address": address or None, "city": _cell(row, cols, "city"), "region": _cell(row, cols, "region"),
                    "postcode": _cell(row, cols, "postcode"), "category": category or cfg.get("source_scope") or "Immigration and asylum",
                })
    finally:
        wb.close()
    result = {"source_id": source_id, "file": path.name, "rows_seen": rows_seen, "rows_valid": len(parsed),
              "rows_skipped": skipped, "sheets_used": sheets_used, "sheets": header_details,
              "dry_run": dry_run, "created": 0, "updated": 0}
    if not parsed:
        raise ValueError("Nenhum registo de imigração foi reconhecido. Não foi feita qualquer gravação.")
    if dry_run:
        result["sample"] = parsed[:5]
        return result
    with connect(db_path) as conn:
        source_exists = conn.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if not source_exists:
            raise RuntimeError(f"Fonte {source_id} não está no Source Registry")
        batch = conn.execute(
            "INSERT INTO import_batches(source_id,filename,dataset_as_of,rows_seen,status) VALUES(?,?,?,?,?)",
            (source_id, path.name, dataset_as_of, rows_seen, "running"),
        ).lastrowid
        for r in parsed:
            oid = None
            if r.get("reference"):
                ident = conn.execute("SELECT organisation_id FROM organisation_identifiers WHERE source_id=? AND namespace='provider_reference' AND identifier=?", (source_id, r["reference"])).fetchone()
                if ident:
                    oid = ident[0]
            if oid is None and r["domain"]:
                row = conn.execute("SELECT organisation_id FROM organisations WHERE domain=? LIMIT 1", (r["domain"],)).fetchone()
                if row:
                    oid = row[0]
            if oid is None:
                row = conn.execute(
                    """SELECT organisation_id FROM organisations
                    WHERE normalized_name=? AND coalesce(address,'')=coalesce(?,'') LIMIT 1""",
                    (normalise_name(r["name"]), r["address"]),
                ).fetchone()
                if row:
                    oid = row[0]
            if oid:
                current = conn.execute(
                    "SELECT provider_categories,immigration_services FROM organisations WHERE organisation_id=?",
                    (oid,),
                ).fetchone()
                categories = _merge_categories(current["provider_categories"], r["category"])
                services = _merge_categories(current["immigration_services"], r["category"])
                conn.execute(
                    """UPDATE organisations SET website=coalesce(website,?),domain=coalesce(domain,?),address=coalesce(address,?),
                    postcode=coalesce(postcode,?),phone=coalesce(phone,?),public_email=coalesce(public_email,?),city=coalesce(city,?),
                    region=coalesce(region,?),provider_categories=?,immigration_services=?,
                    last_seen_at=CURRENT_TIMESTAMP,source_quality=max(source_quality,0.78) WHERE organisation_id=?""",
                    (r["website"], r["domain"], r["address"], r["postcode"], r["phone"], r["email"], r["city"], r["region"],
                     categories, services, oid),
                )
                result["updated"] += 1
            else:
                cur = conn.execute(
                    """INSERT INTO organisations(canonical_name,normalized_name,website,domain,organisation_type,address,postcode,phone,public_email,city,region,provider_categories,immigration_services,profile_status,confidence,source_quality)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'imported',0.78,0.78)""",
                    (r["name"], normalise_name(r["name"]), r["website"], r["domain"], cfg.get("provider_type", "legal_aid_provider"),
                     r["address"], r["postcode"], r["phone"], r["email"], r["city"], r["region"], r["category"], r["category"]),
                )
                oid = cur.lastrowid
                result["created"] += 1
            if r.get("reference"):
                conn.execute("INSERT INTO organisation_identifiers(organisation_id,source_id,namespace,identifier,source_url) VALUES(?,?,?,?,?) ON CONFLICT(source_id,namespace,identifier) DO UPDATE SET organisation_id=excluded.organisation_id,source_url=excluded.source_url",
                             (oid, source_id, "provider_reference", r["reference"], source_url))
            for field in ("name", "reference", "website", "address", "postcode", "phone", "email", "category"):
                val = r.get(field)
                if val:
                    _evidence(conn, oid, source_id, source_url, field, val, published_at, dataset_as_of)
        conn.execute(
            "UPDATE import_batches SET organisations_created=?,organisations_updated=?,rows_skipped=?,status='completed' WHERE batch_id=?",
            (result["created"], result["updated"], skipped, batch),
        )
    result["batch_id"] = batch
    return result


def collect_dataset_source(source: dict, db_path: str, *, timeout: int = 20, dry_run: bool = False) -> dict:
    """Download a current official dataset and send it through the matching importer."""
    from .normalise import clean
    downloaded = download_govuk_xlsx(source, db_path, timeout=timeout)
    cfg = source.get("config", {})
    if cfg.get("importer") == "iaa":
        result = import_iaa_xlsx(downloaded["file"], db_path, dry_run=dry_run,
                                 dataset_as_of=cfg.get("dataset_as_of"), published_at=downloaded.get("published_at") or cfg.get("published_at"))
    else:
        result = import_provider_workbook(downloaded["file"], db_path, source, dry_run=dry_run,
                                          published_at=downloaded.get("published_at") or cfg.get("published_at"),
                                          dataset_as_of=cfg.get("dataset_as_of"))
    result["download"] = {k: v for k, v in downloaded.items() if k != "file"}
    return result
