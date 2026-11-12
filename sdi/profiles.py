from __future__ import annotations
from .db import connect

def get_profile(organisation_id: int, db_path: str) -> dict | None:
    with connect(db_path) as conn:
        row=conn.execute("SELECT * FROM organisations WHERE organisation_id=?",(organisation_id,)).fetchone()
        if not row: return None
        evidence=conn.execute("SELECT source_id,source_url,field_name,field_value,collected_at,source_published_at,dataset_as_of,evidence_status,confidence,source_authority FROM evidence WHERE organisation_id=? ORDER BY collected_at DESC",(organisation_id,)).fetchall()
        pages=conn.execute("SELECT url,final_url,http_status,title,description,fetched_at,crawl_status FROM crawled_pages WHERE organisation_id=? ORDER BY fetched_at DESC",(organisation_id,)).fetchall()
        return {"organisation":dict(row),"evidence":[dict(x) for x in evidence],"pages":[dict(x) for x in pages]}

def list_profiles(db_path: str, limit: int=100) -> list[dict]:
    with connect(db_path) as conn:
        rows=conn.execute("SELECT organisation_id,canonical_name,website,domain,city,organisation_type,regulator,regulator_status,profile_status,confidence,last_seen_at FROM organisations ORDER BY canonical_name LIMIT ?",(limit,)).fetchall()
        return [dict(r) for r in rows]
