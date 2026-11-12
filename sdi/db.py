from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = r'''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sources (
 source_id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
 category TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 50,
 access_method TEXT NOT NULL, status TEXT NOT NULL, notes TEXT,
 source_type TEXT NOT NULL DEFAULT 'manual', config_json TEXT NOT NULL DEFAULT '{}',
 last_success_at TEXT, last_error TEXT, records_seen INTEGER NOT NULL DEFAULT 0,
 records_accepted INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS organisations (
 organisation_id INTEGER PRIMARY KEY AUTOINCREMENT,
 canonical_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 website TEXT, domain TEXT, organisation_type TEXT,
 country TEXT NOT NULL DEFAULT 'United Kingdom', region TEXT, city TEXT,
 address TEXT, postcode TEXT, phone TEXT, public_email TEXT,
 immigration_services TEXT, provider_categories TEXT,
 regulator TEXT, regulator_reference TEXT, regulator_status TEXT,
 profile_status TEXT NOT NULL DEFAULT 'discovered', confidence REAL NOT NULL DEFAULT 0,
 source_quality REAL NOT NULL DEFAULT 0, website_last_crawled_at TEXT, website_last_attempt_at TEXT, website_crawl_status TEXT,
 first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(normalized_name, domain)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_org_regulator_ref ON organisations(regulator, regulator_reference)
 WHERE regulator_reference IS NOT NULL AND regulator_reference <> '';
CREATE INDEX IF NOT EXISTS idx_org_domain ON organisations(domain);
CREATE INDEX IF NOT EXISTS idx_org_name ON organisations(normalized_name);
CREATE TABLE IF NOT EXISTS evidence (
 evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
 organisation_id INTEGER NOT NULL REFERENCES organisations(organisation_id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES sources(source_id), source_url TEXT NOT NULL,
 field_name TEXT NOT NULL, field_value TEXT NOT NULL,
 collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 source_published_at TEXT, dataset_as_of TEXT,
 evidence_status TEXT NOT NULL DEFAULT 'observed', content_hash TEXT,
 UNIQUE(organisation_id, source_id, source_url, field_name, field_value)
);
CREATE INDEX IF NOT EXISTS idx_evidence_org ON evidence(organisation_id);
CREATE TABLE IF NOT EXISTS api_clients (
 client_id INTEGER PRIMARY KEY AUTOINCREMENT,
 client_name TEXT NOT NULL UNIQUE,
 key_hash TEXT NOT NULL,
 scopes TEXT NOT NULL DEFAULT 'organisations:read',
 is_active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS organisation_identifiers (
 identifier_id INTEGER PRIMARY KEY AUTOINCREMENT,
 organisation_id INTEGER NOT NULL REFERENCES organisations(organisation_id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES sources(source_id),
 namespace TEXT NOT NULL, identifier TEXT NOT NULL,
 source_url TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(source_id,namespace,identifier)
);
CREATE INDEX IF NOT EXISTS idx_org_identifiers_org ON organisation_identifiers(organisation_id);
CREATE TABLE IF NOT EXISTS import_batches (
 batch_id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL REFERENCES sources(source_id),
 filename TEXT NOT NULL, dataset_as_of TEXT, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 rows_seen INTEGER NOT NULL DEFAULT 0, organisations_created INTEGER NOT NULL DEFAULT 0,
 organisations_updated INTEGER NOT NULL DEFAULT 0, rows_skipped INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'completed', notes TEXT
);
CREATE TABLE IF NOT EXISTS crawled_pages (
 page_id INTEGER PRIMARY KEY AUTOINCREMENT,
 organisation_id INTEGER REFERENCES organisations(organisation_id) ON DELETE SET NULL,
 source_id TEXT NOT NULL REFERENCES sources(source_id), url TEXT NOT NULL UNIQUE,
 final_url TEXT, http_status INTEGER, title TEXT, description TEXT, text_content TEXT,
 content_hash TEXT, fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 crawl_status TEXT NOT NULL, error TEXT, depth INTEGER NOT NULL DEFAULT 0,
 is_indexable INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_pages_org ON crawled_pages(organisation_id);
CREATE TABLE IF NOT EXISTS discovery_runs (
 run_id INTEGER PRIMARY KEY AUTOINCREMENT,
 seed_url TEXT NOT NULL,
 source_id TEXT,
 run_type TEXT NOT NULL DEFAULT 'source_discovery',
 started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 finished_at TEXT,
 pages_checked INTEGER NOT NULL DEFAULT 0,
 candidates_found INTEGER NOT NULL DEFAULT 0,
 organisations_created INTEGER NOT NULL DEFAULT 0,
 organisations_updated INTEGER NOT NULL DEFAULT 0,
 datasets_imported INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'running',
 error TEXT
);
CREATE TABLE IF NOT EXISTS organisation_candidates (
 candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id INTEGER NOT NULL REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
 seed_url TEXT NOT NULL,
 candidate_url TEXT NOT NULL,
 domain TEXT NOT NULL,
 anchor_text TEXT,
 page_title TEXT,
 description TEXT,
 relevance_score REAL NOT NULL DEFAULT 0,
 decision TEXT NOT NULL DEFAULT 'pending',
 decision_reason TEXT,
 organisation_id INTEGER REFERENCES organisations(organisation_id) ON DELETE SET NULL,
 checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(run_id, domain)
);
CREATE INDEX IF NOT EXISTS idx_candidates_domain ON organisation_candidates(domain);
CREATE INDEX IF NOT EXISTS idx_candidates_decision ON organisation_candidates(decision);
CREATE TABLE IF NOT EXISTS discovered_urls (
 url_id INTEGER PRIMARY KEY AUTOINCREMENT, parent_url TEXT NOT NULL,
 discovered_url TEXT NOT NULL UNIQUE, domain TEXT NOT NULL,
 discovery_type TEXT NOT NULL DEFAULT 'link', status TEXT NOT NULL DEFAULT 'queued',
 anchor_text TEXT, discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS discovery_jobs (
 job_id INTEGER PRIMARY KEY AUTOINCREMENT, job_type TEXT NOT NULL DEFAULT 'full_discovery',
 status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 started_at TEXT, finished_at TEXT, result_json TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON discovery_jobs(status,created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS organisations_fts USING fts5(
 canonical_name, domain, city, organisation_type, immigration_services,
 content='organisations', content_rowid='organisation_id'
);
CREATE TRIGGER IF NOT EXISTS organisations_ai AFTER INSERT ON organisations BEGIN
 INSERT INTO organisations_fts(rowid, canonical_name, domain, city, organisation_type, immigration_services)
 VALUES (new.organisation_id, new.canonical_name, coalesce(new.domain,''), coalesce(new.city,''), coalesce(new.organisation_type,''), coalesce(new.immigration_services,''));
END;
CREATE TRIGGER IF NOT EXISTS organisations_ad AFTER DELETE ON organisations BEGIN
 INSERT INTO organisations_fts(organisations_fts, rowid, canonical_name, domain, city, organisation_type, immigration_services)
 VALUES ('delete', old.organisation_id, old.canonical_name, old.domain, old.city, old.organisation_type, old.immigration_services);
END;
CREATE TRIGGER IF NOT EXISTS organisations_au AFTER UPDATE ON organisations BEGIN
 INSERT INTO organisations_fts(organisations_fts, rowid, canonical_name, domain, city, organisation_type, immigration_services)
 VALUES ('delete', old.organisation_id, old.canonical_name, old.domain, old.city, old.organisation_type, old.immigration_services);
 INSERT INTO organisations_fts(rowid, canonical_name, domain, city, organisation_type, immigration_services)
 VALUES (new.organisation_id, new.canonical_name, coalesce(new.domain,''), coalesce(new.city,''), coalesce(new.organisation_type,''), coalesce(new.immigration_services,''));
END;
CREATE VIRTUAL TABLE IF NOT EXISTS sdi_org_search USING fts5(
 canonical_name, domain, city, region, address, organisation_type, immigration_services,
 provider_categories, regulator, regulator_reference, public_email, phone,
 organisation_id UNINDEXED
);
CREATE VIRTUAL TABLE IF NOT EXISTS sdi_page_search USING fts5(
 title, description, text_content, url UNINDEXED, organisation_id UNINDEXED, page_id UNINDEXED
);
CREATE TABLE IF NOT EXISTS sdi_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS sdi_org_ai AFTER INSERT ON organisations BEGIN
 INSERT INTO sdi_org_search(rowid,canonical_name,domain,city,region,address,organisation_type,immigration_services,provider_categories,regulator,regulator_reference,public_email,phone,organisation_id)
 VALUES(new.organisation_id,new.canonical_name,coalesce(new.domain,''),coalesce(new.city,''),coalesce(new.region,''),coalesce(new.address,''),coalesce(new.organisation_type,''),coalesce(new.immigration_services,''),coalesce(new.provider_categories,''),coalesce(new.regulator,''),coalesce(new.regulator_reference,''),coalesce(new.public_email,''),coalesce(new.phone,''),new.organisation_id);
END;
CREATE TRIGGER IF NOT EXISTS sdi_org_ad AFTER DELETE ON organisations BEGIN
 DELETE FROM sdi_org_search WHERE rowid=old.organisation_id;
END;
CREATE TRIGGER IF NOT EXISTS sdi_org_au AFTER UPDATE ON organisations BEGIN
 DELETE FROM sdi_org_search WHERE rowid=old.organisation_id;
 INSERT INTO sdi_org_search(rowid,canonical_name,domain,city,region,address,organisation_type,immigration_services,provider_categories,regulator,regulator_reference,public_email,phone,organisation_id)
 VALUES(new.organisation_id,new.canonical_name,coalesce(new.domain,''),coalesce(new.city,''),coalesce(new.region,''),coalesce(new.address,''),coalesce(new.organisation_type,''),coalesce(new.immigration_services,''),coalesce(new.provider_categories,''),coalesce(new.regulator,''),coalesce(new.regulator_reference,''),coalesce(new.public_email,''),coalesce(new.phone,''),new.organisation_id);
END;
CREATE TRIGGER IF NOT EXISTS sdi_page_ai AFTER INSERT ON crawled_pages WHEN new.is_indexable=1 BEGIN
 INSERT INTO sdi_page_search(rowid,title,description,text_content,url,organisation_id,page_id)
 VALUES(new.page_id,coalesce(new.title,''),coalesce(new.description,''),coalesce(new.text_content,''),new.url,coalesce(new.organisation_id,0),new.page_id);
END;
CREATE TRIGGER IF NOT EXISTS sdi_page_au AFTER UPDATE ON crawled_pages BEGIN
 DELETE FROM sdi_page_search WHERE rowid=old.page_id;
 INSERT INTO sdi_page_search(rowid,title,description,text_content,url,organisation_id,page_id)
 SELECT new.page_id,coalesce(new.title,''),coalesce(new.description,''),coalesce(new.text_content,''),new.url,coalesce(new.organisation_id,0),new.page_id
 WHERE new.is_indexable=1;
END;
CREATE TRIGGER IF NOT EXISTS sdi_page_ad AFTER DELETE ON crawled_pages BEGIN
 DELETE FROM sdi_page_search WHERE rowid=old.page_id;
END;
'''

@contextmanager
def connect(path: str = "data/sdi.sqlite3"):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialise(path: str = "data/sdi.sqlite3") -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        migrations = {
            "sources": {
                "source_type": "TEXT NOT NULL DEFAULT 'manual'", "config_json": "TEXT NOT NULL DEFAULT '{}'",
                "last_success_at": "TEXT", "last_error": "TEXT", "records_seen": "INTEGER NOT NULL DEFAULT 0",
                "records_accepted": "INTEGER NOT NULL DEFAULT 0",
            },
            "organisations": {
                "postcode": "TEXT", "provider_categories": "TEXT", "source_quality": "REAL NOT NULL DEFAULT 0",
                "website_last_crawled_at": "TEXT", "website_last_attempt_at": "TEXT", "website_crawl_status": "TEXT",
            },
            "crawled_pages": {"is_indexable": "INTEGER NOT NULL DEFAULT 1", "duplicate_of_url": "TEXT"},
            "evidence": {"confidence": "REAL NOT NULL DEFAULT 0.5", "source_authority": "TEXT"},
            "discovered_urls": {"anchor_text": "TEXT", "source_id": "TEXT", "depth": "INTEGER NOT NULL DEFAULT 0", "attempts": "INTEGER NOT NULL DEFAULT 0", "last_attempt_at": "TEXT", "last_error": "TEXT"},
            "discovery_runs": {"source_id": "TEXT", "run_type": "TEXT NOT NULL DEFAULT 'source_discovery'", "datasets_imported": "INTEGER NOT NULL DEFAULT 0"},
        }
        for table, additions in migrations.items():
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, declaration in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_org_website_crawl ON organisations(website_last_crawled_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_org_website_attempt ON organisations(website_last_attempt_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered_urls_status ON discovered_urls(status,discovered_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered_urls_source_queue ON discovered_urls(source_id,status,depth,attempts)")
        conn.execute("""CREATE TABLE IF NOT EXISTS page_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL REFERENCES crawled_pages(page_id) ON DELETE CASCADE,
            organisation_id INTEGER REFERENCES organisations(organisation_id) ON DELETE SET NULL,
            source_id TEXT NOT NULL,
            url TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            title TEXT,
            description TEXT,
            text_content TEXT,
            change_type TEXT NOT NULL DEFAULT 'new',
            captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(page_id,content_hash)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_versions_page_time ON page_versions(page_id,captured_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_versions_org_time ON page_versions(organisation_id,captured_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_field ON evidence(organisation_id,field_name,collected_at)")
        conn.execute("""UPDATE evidence SET confidence=CASE evidence_status WHEN 'official_snapshot' THEN 0.90 WHEN 'official_register' THEN 0.95 WHEN 'website_observation' THEN 0.60 WHEN 'discovery_observation' THEN 0.55 ELSE 0.50 END WHERE confidence=0.5""")
        # One-time initial build of the expanded search index for databases upgraded from 0.7.
        marker = conn.execute("SELECT value FROM sdi_meta WHERE key='search_index_version'").fetchone()
        if not marker or marker[0] != "2":
            conn.execute("DELETE FROM sdi_org_search")
            conn.execute("""INSERT INTO sdi_org_search(rowid,canonical_name,domain,city,region,address,organisation_type,immigration_services,provider_categories,regulator,regulator_reference,public_email,phone,organisation_id)
                SELECT organisation_id,canonical_name,coalesce(domain,''),coalesce(city,''),coalesce(region,''),coalesce(address,''),coalesce(organisation_type,''),coalesce(immigration_services,''),coalesce(provider_categories,''),coalesce(regulator,''),coalesce(regulator_reference,''),coalesce(public_email,''),coalesce(phone,''),organisation_id FROM organisations""")
            conn.execute("DELETE FROM sdi_page_search")
            conn.execute("""INSERT INTO sdi_page_search(rowid,title,description,text_content,url,organisation_id,page_id)
                SELECT page_id,coalesce(title,''),coalesce(description,''),coalesce(text_content,''),url,coalesce(organisation_id,0),page_id FROM crawled_pages WHERE is_indexable=1""")
            conn.execute("INSERT OR REPLACE INTO sdi_meta(key,value) VALUES('search_index_version','2')")
