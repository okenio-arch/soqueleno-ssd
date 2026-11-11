from __future__ import annotations
from pathlib import Path
from .db import connect
from .normalise import clean, normalise_name, normalise_url, domain_of

IAA_SOURCE_ID = "iaa_foi_register"
IAA_SOURCE_URL = "https://www.gov.uk/government/publications/register-of-currently-registered-immigration-advice-organisations"
DATASET_AS_OF = "2026-07-09"
PUBLISHED_AT = "2026-09-25"
ALIASES = {
 "name": {"organisation name","organisation","name","registered organisation name","business name"},
 "reference": {"registration number","registration no","registration reference","organisation registration number","iaa number","oisc number","reference number"},
 "level": {"registration level","level","authorisation level"},
 "status": {"authorisation status","authorisation","status","registration status"},
 "website": {"website","web site","website address"},
 "address": {"address","registered address","business address"},
 "phone": {"telephone","telephone number","phone","telephone no"},
 "email": {"email","email address","public email"},
}

def _key(v): return " ".join(str(v or "").strip().casefold().replace("_"," ").split())
def _detect(headers):
    result = {}
    for idx, header in enumerate(headers):
        key = _key(header)
        for field, aliases in ALIASES.items():
            if key in aliases and field not in result: result[field] = idx
    return result

def inspect_workbook(path: str) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    result = {"file": Path(path).name, "sheets": []}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(min_row=1, max_row=15, values_only=True))
        result["sheets"].append({"name": ws.title, "sample_rows": len(rows), "candidate_headers": [list(r) for r in rows if any(v is not None for v in r)][:5]})
    wb.close()
    return result

def import_iaa_xlsx(xlsx_path: str, db_path: str, dry_run: bool = False, *, dataset_as_of: str | None = None, published_at: str | None = None) -> dict:
    from openpyxl import load_workbook
    dataset_as_of = dataset_as_of or DATASET_AS_OF
    published_at = published_at or PUBLISHED_AT
    path = Path(xlsx_path)
    if not path.is_file(): raise FileNotFoundError(path)
    wb = load_workbook(path, read_only=True, data_only=True)
    found = None
    for ws in wb.worksheets:
        for row_num, row in enumerate(ws.iter_rows(min_row=1, max_row=20, values_only=True), 1):
            headers = [clean(v) or "" for v in row]
            cols = _detect(headers)
            if "name" in cols and "reference" in cols:
                found = (ws, row_num, cols); break
        if found: break
    if not found:
        wb.close(); raise ValueError("Não foram reconhecidos cabeçalhos para nome e referência IAA. Inspecione o XLSX e ajuste aliases antes de importar.")
    ws, header_row, cols = found
    records=[]; seen=0; skipped=0
    for row in ws.iter_rows(min_row=header_row+1, values_only=True):
        seen += 1
        rec={k: clean(row[i] if i < len(row) else None) for k,i in cols.items()}
        if not rec.get("name") or not rec.get("reference"):
            skipped += 1; continue
        rec["website"] = normalise_url(rec.get("website"))
        records.append(rec)
    wb.close()
    result={"rows_seen":seen,"rows_valid":len(records),"rows_skipped":skipped,"sheet":ws.title,"header_row":header_row,"dry_run":dry_run,"created":0,"updated":0}
    if dry_run: return result
    with connect(db_path) as conn:
        if not conn.execute("SELECT 1 FROM sources WHERE source_id=?",(IAA_SOURCE_ID,)).fetchone():
            raise RuntimeError("Fonte IAA não registada. Execute load-sources.")
        batch=conn.execute("INSERT INTO import_batches(source_id,filename,dataset_as_of,rows_seen,status) VALUES(?,?,?,?,?)",(IAA_SOURCE_ID,path.name,dataset_as_of,seen,"running")).lastrowid
        for r in records:
            existing=conn.execute("SELECT organisation_id FROM organisations WHERE regulator='IAA' AND regulator_reference=?",(r["reference"],)).fetchone()
            domain=domain_of(r.get("website")); norm=normalise_name(r["name"])
            # Resolve an IAA entry to an organisation previously imported from LAA
            # before inserting a second profile for the same firm.
            if not existing and domain:
                existing=conn.execute("SELECT organisation_id FROM organisations WHERE domain=? LIMIT 1",(domain,)).fetchone()
            if not existing and r.get("address"):
                existing=conn.execute("SELECT organisation_id FROM organisations WHERE normalized_name=? AND coalesce(address,'')=? LIMIT 1",(norm,r["address"])).fetchone()
            if existing:
                oid=existing[0]
                conn.execute("""UPDATE organisations SET canonical_name=?,normalized_name=?,website=COALESCE(?,website),domain=COALESCE(?,domain),
                organisation_type=COALESCE(?,organisation_type),address=COALESCE(?,address),phone=COALESCE(?,phone),public_email=COALESCE(?,public_email),
                regulator=CASE WHEN regulator IS NULL OR regulator='IAA' THEN 'IAA' ELSE regulator END,
                regulator_reference=CASE WHEN regulator IS NULL OR regulator='IAA' THEN ? ELSE regulator_reference END,
                regulator_status=CASE WHEN regulator IS NULL OR regulator='IAA' THEN COALESCE(?,regulator_status) ELSE regulator_status END,
                confidence=max(confidence,0.75),last_seen_at=CURRENT_TIMESTAMP WHERE organisation_id=?""",
                (r["name"],norm,r.get("website"),domain,r.get("level"),r.get("address"),r.get("phone"),r.get("email"),r["reference"],r.get("status"),oid))
                result["updated"]+=1
            else:
                cur=conn.execute("""INSERT INTO organisations(canonical_name,normalized_name,website,domain,organisation_type,address,phone,public_email,regulator,regulator_reference,regulator_status,confidence)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(r["name"],norm,r.get("website"),domain,r.get("level"),r.get("address"),r.get("phone"),r.get("email"),"IAA",r["reference"],r.get("status"),0.75))
                oid=cur.lastrowid; result["created"]+=1
            conn.execute("INSERT INTO organisation_identifiers(organisation_id,source_id,namespace,identifier,source_url) VALUES(?,?,?,?,?) ON CONFLICT(source_id,namespace,identifier) DO UPDATE SET organisation_id=excluded.organisation_id,source_url=excluded.source_url",
                         (oid,IAA_SOURCE_ID,"iaa_registration",r["reference"],IAA_SOURCE_URL))
            fields={"canonical_name":r["name"],"regulator_reference":r["reference"],"registration_level":r.get("level"),"regulator_status":r.get("status"),"website":r.get("website"),"address":r.get("address"),"phone":r.get("phone"),"public_email":r.get("email")}
            for field,value in fields.items():
                if value:
                    conn.execute("INSERT OR IGNORE INTO evidence(organisation_id,source_id,source_url,field_name,field_value,source_published_at,dataset_as_of,evidence_status) VALUES(?,?,?,?,?,?,?,?)",(oid,IAA_SOURCE_ID,IAA_SOURCE_URL,field,value,published_at,dataset_as_of,"official_snapshot"))
        conn.execute("UPDATE import_batches SET organisations_created=?,organisations_updated=?,rows_skipped=?,status='completed' WHERE batch_id=?",(result["created"],result["updated"],skipped,batch))
    result["batch_id"]=batch
    return result
