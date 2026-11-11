from __future__ import annotations
import argparse, secrets, hashlib
from pathlib import Path
from sdi.config import ROOT, db_path, setting
from sdi.db import initialise, connect
from sdi.registry import load_sources
from sdi.iaa_import import inspect_workbook, import_iaa_xlsx
from sdi.crawler import crawl_organisation
from sdi.discovery import discover_from_seed, discover_all_sources
from sdi.search import search_organisations
from sdi.profiles import list_profiles

def main():
    p=argparse.ArgumentParser(description="Soqueleno Discovery Infrastructure")
    p.add_argument("command",choices=["init","load-sources","inspect-iaa","import-iaa","crawl","discover","search","list","create-key","serve"])
    p.add_argument("--db",default=db_path()); p.add_argument("--file"); p.add_argument("--dry-run",action="store_true")
    p.add_argument("--organisation-id",type=int); p.add_argument("--url"); p.add_argument("--query"); p.add_argument("--limit",type=int,default=25)
    p.add_argument("--max-pages",type=int,default=int(setting("SDI_MAX_PAGES_PER_SITE","20")))
    p.add_argument("--max-depth",type=int,default=int(setting("SDI_MAX_DEPTH","2"))); p.add_argument("--max-candidates",type=int,default=15)
    p.add_argument("--client",default="scout",help="Nome do consumidor para create-key, ex.: scout ou auren")
    args=p.parse_args()
    if args.command=="init":
        initialise(args.db); print(f"Base SDI inicializada: {args.db}")
    elif args.command=="load-sources":
        initialise(args.db); n=load_sources(str(ROOT/"config"/"sources.json"),args.db); print(f"{n} fontes registadas")
    elif args.command=="inspect-iaa":
        if not args.file: p.error("use --file")
        import json; print(json.dumps(inspect_workbook(args.file),ensure_ascii=False,indent=2))
    elif args.command=="import-iaa":
        if not args.file: p.error("use --file")
        initialise(args.db); print(import_iaa_xlsx(args.file,args.db,args.dry_run))
    elif args.command=="crawl":
        if not args.organisation_id or not args.url: p.error("crawl requer --organisation-id e --url")
        print(crawl_organisation(args.organisation_id,args.url,args.db,max_pages=args.max_pages,max_depth=args.max_depth,timeout=int(setting("SDI_REQUEST_TIMEOUT","12")),delay=float(setting("SDI_REQUEST_DELAY_SECONDS","1.5"))))
    elif args.command=="discover":
        import json
        if args.url:
            result=discover_from_seed(args.url,args.db,max_candidates=max(1,min(args.max_candidates,50)),timeout=int(setting("SDI_REQUEST_TIMEOUT","12")),delay=float(setting("SDI_REQUEST_DELAY_SECONDS","1.5")))
        else:
            result=discover_all_sources(args.db,timeout=int(setting("SDI_REQUEST_TIMEOUT","12")),delay=float(setting("SDI_REQUEST_DELAY_SECONDS","1.5")),max_candidates_per_source=max(1,min(args.max_candidates,50)))
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command=="search":
        if not args.query: p.error("search requer --query")
        print(search_organisations(args.db,args.query,limit=args.limit))
    elif args.command=="list": print(list_profiles(args.db,args.limit))
    elif args.command=="create-key":
        initialise(args.db)
        key=secrets.token_urlsafe(40); digest=hashlib.sha256(key.encode("utf-8")).hexdigest()
        with connect(args.db) as conn:
            conn.execute("INSERT INTO api_clients(client_name,key_hash,scopes,is_active) VALUES(?,?,?,1) ON CONFLICT(client_name) DO UPDATE SET key_hash=excluded.key_hash,scopes=excluded.scopes,is_active=1,created_at=CURRENT_TIMESTAMP",(args.client,digest,"organisations:read"))
        print(f"Credencial criada para: {args.client}")
        print("Copia esta chave agora. A SDI guarda apenas o hash e não consegue voltar a mostrá-la:")
        print(key)
    elif args.command=="serve":
        import uvicorn
        initialise(args.db)
        uvicorn.run("sdi.api:app",host=setting("SDI_HOST","127.0.0.1"),port=int(setting("SDI_PORT","8100")),reload=False)

if __name__=="__main__": main()
