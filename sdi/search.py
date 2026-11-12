from __future__ import annotations
import re
from .db import connect

def _fts_query(query: str) -> str:
    """Build a safe FTS5 prefix query from user text, never raw FTS syntax."""
    tokens = re.findall(r"[\w@.+-]+", (query or "").casefold(), flags=re.UNICODE)
    tokens = [token.strip(".+-@_") for token in tokens]
    tokens = [token for token in tokens if token]
    if not tokens:
        raise ValueError("A pesquisa precisa de conter pelo menos uma palavra ou número")
    return " AND ".join(f'"{token}"*' for token in tokens[:12])

def _limit(value: int, maximum: int = 100) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return 25

def search_organisations(db_path: str, query: str, *, limit: int = 25, city: str | None = None,
                         regulator: str | None = None) -> list[dict]:
    fts = _fts_query(query)
    sql = """SELECT o.organisation_id,o.canonical_name,o.website,o.domain,o.city,o.region,o.postcode,
    o.organisation_type,o.immigration_services,o.provider_categories,o.regulator,o.regulator_reference,
    o.regulator_status,o.profile_status,o.confidence,o.source_quality,o.address,o.phone,o.public_email,
    bm25(sdi_org_search,10.0,3.0,1.0,1.0,0.5,2.0,4.0,3.0,3.0,1.0,0.2,0.1,0.0) AS rank
    FROM sdi_org_search JOIN organisations o ON o.organisation_id=sdi_org_search.organisation_id
    WHERE sdi_org_search MATCH ?"""
    params: list[object] = [fts]
    if city:
        sql += " AND lower(coalesce(o.city,'')) LIKE ?"
        params.append(f"%{city.casefold()}%")
    if regulator:
        sql += " AND o.regulator=?"
        params.append(regulator)
    sql += " ORDER BY rank LIMIT ?"
    params.append(_limit(limit))
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

def search_everything(db_path: str, query: str, *, limit: int = 30, city: str | None = None,
                      regulator: str | None = None) -> dict:
    fts = _fts_query(query)
    limit = _limit(limit)
    org_sql = """SELECT o.organisation_id,o.canonical_name,o.website,o.domain,o.city,o.region,o.postcode,
    o.organisation_type,o.immigration_services,o.provider_categories,o.regulator,o.regulator_reference,
    o.regulator_status,o.profile_status,o.confidence,o.source_quality,o.address,o.phone,o.public_email,
    bm25(sdi_org_search,10.0,3.0,1.0,1.0,0.5,2.0,4.0,3.0,3.0,1.0,0.2,0.1,0.0) AS rank
    FROM sdi_org_search JOIN organisations o ON o.organisation_id=sdi_org_search.organisation_id
    WHERE sdi_org_search MATCH ?"""
    org_params: list[object] = [fts]
    page_sql = """SELECT p.page_id,p.organisation_id,p.url,p.title,p.description,
    snippet(sdi_page_search,2,'<mark>','</mark>',' … ',28) AS snippet,
    o.canonical_name,o.website,o.domain,bm25(sdi_page_search,8.0,4.0,1.0,0.0,0.0,0.0) AS rank
    FROM sdi_page_search JOIN crawled_pages p ON p.page_id=sdi_page_search.page_id
    LEFT JOIN organisations o ON o.organisation_id=p.organisation_id
    WHERE sdi_page_search MATCH ? AND p.is_indexable=1"""
    page_params: list[object] = [fts]
    if city:
        org_sql += " AND lower(coalesce(o.city,'')) LIKE ?"
        org_params.append(f"%{city.casefold()}%")
        page_sql += " AND lower(coalesce(o.city,'')) LIKE ?"
        page_params.append(f"%{city.casefold()}%")
    if regulator:
        org_sql += " AND o.regulator=?"
        org_params.append(regulator)
        page_sql += " AND o.regulator=?"
        page_params.append(regulator)
    org_sql += " ORDER BY rank LIMIT ?"
    org_params.append(limit)
    page_sql += " ORDER BY rank LIMIT ?"
    page_params.append(limit * 2)
    with connect(db_path) as conn:
        organisations = [dict(row) for row in conn.execute(org_sql, org_params).fetchall()]
        pages = [dict(row) for row in conn.execute(page_sql, page_params).fetchall()]
        sources = conn.execute("SELECT count(*) FROM sources WHERE status='enabled'").fetchone()[0]
        indexed_pages = conn.execute("SELECT count(*) FROM crawled_pages WHERE is_indexable=1 AND crawl_status='success'").fetchone()[0]
    return {"query": query, "organisations": organisations, "pages": pages,
            "meta": {"organisations_returned": len(organisations), "pages_returned": len(pages),
                     "enabled_sources": sources, "indexed_pages": indexed_pages}}
