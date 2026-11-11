from __future__ import annotations

import json
from pathlib import Path
from .db import connect


def load_sources(json_path: str, db_path: str) -> int:
    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    items = payload.get("sources", [])
    with connect(db_path) as conn:
        for item in items:
            conn.execute(
                """INSERT INTO sources(source_id,name,url,category,priority,access_method,status,notes,source_type,config_json)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                name=excluded.name,url=excluded.url,category=excluded.category,priority=excluded.priority,
                access_method=excluded.access_method,notes=excluded.notes,source_type=excluded.source_type,
                config_json=excluded.config_json,status=CASE WHEN sources.status IN ('blocked_pending_authorisation','disabled','review_required') THEN sources.status ELSE excluded.status END,updated_at=CURRENT_TIMESTAMP""",
                (item["id"], item["name"], item["url"], item["category"], item.get("priority", 50),
                 item["access_method"], item.get("status", "enabled"), item.get("notes", ""),
                 item.get("source_type", "manual"), json.dumps(item.get("config", {}), ensure_ascii=False)),
            )
    return len(items)


def get_source_config(row) -> dict:
    try:
        return json.loads(row["config_json"] or "{}")
    except (TypeError, ValueError, KeyError):
        return {}
