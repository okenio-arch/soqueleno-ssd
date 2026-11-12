# SDI engineering status and source policy

## Purpose

The SDI is a focused UK legal-provider discovery and evidence index for Scout. It is not a general-purpose search engine and does not crawl the whole Internet. Its job is to collect permitted source data, enrich known provider websites, retain field-level evidence and return traceable results.

## Source readiness

| Source | Current state | What it can establish | Constraint |
|---|---|---|---|
| IAA FOI organisation register | Enabled snapshot importer | Organisation name, IAA reference, level, website and other fields present in the workbook | Snapshot position is 9 July 2026. It is not live authorisation status. |
| LAA provider directory | Enabled monthly XLSX importer | Providers with immigration/asylum legal-aid categories | England and Wales; legal-aid coverage is not the whole immigration market. |
| LAA South West remote-access directory | Enabled regional XLSX importer | Remote-access providers in its stated regional scope | Not a UK-wide list. |
| IAA Adviser Finder | Blocked | Would be the most relevant live IAA directory | Official notice says Finder and Register are temporarily unavailable; re-check before enabling. |
| SRA Data Share API V1 | Blocked pending account/key | Regulated law firms, authorisation fields, websites and areas of law | Free API access, but registration/subscription key and mandatory public attribution are required. Adapter is not activated without credentials and validation. |
| Law Society Find a Solicitor | Blocked | Solicitor directory | Existing terms review found restrictions incompatible with automated commercial prospect-list reuse without written permission. |

No HTML directory seed is currently enabled. This is intentional: a recursive crawler without a permitted, high-quality seed only multiplies noise. The IAA Finder remains blocked, and the SRA API cannot be called until the operator has a key.

## Evidence model

Every imported fact should be represented in `evidence` with:

- organisation and field name/value;
- source ID and source URL;
- collection time;
- publisher date and dataset-as-of date when supplied;
- evidence status, distinguishing official snapshots from website observations;
- confidence score and source authority label.

Confidence values are engineering weights, not calibrated probabilities. Current defaults: official snapshots 0.90, discovery observations 0.55 and website observations 0.60. Scout must inspect evidence status and age; it must never convert an old snapshot into a live regulatory-status claim.

## Crawler controls

- HTTP(S) only; credentials and non-standard ports rejected.
- DNS answers must be public; the socket connects directly to one validated IP, while the original Host header and TLS SNI/certificate hostname are preserved. The connected peer is checked against the pinned IP.
- Ambient proxy configuration is disabled for crawler clients.
- Response bodies are streamed and capped: 512 KB for robots.txt, 2 MB for HTML/sitemaps, and 35 MB for official XLSX downloads.
- Redirects are explicit, bounded and checked against source host allowlists where applicable.
- robots.txt failures are fail-closed for 401/403/429, server errors and fetch failures. A genuine 404 means no robots rules were published.
- Sitemap indexes are followed with a five-sitemap cap; page URLs remain subject to the page budget and robots rules.
- Tracking parameters and fragments are removed from page identity; functional query parameters are preserved.
- Identical page content for the same organisation is stored as a duplicate and excluded from the search index.

## Recursive discovery and recovery

Recursive discovery is breadth-first and bounded by depth, domain and candidate limits. Queue rows retain source, parent, depth, attempts, last-attempt time and last error. Interrupted processing rows are returned to the queue on the next run. Failed candidates receive at most three attempts across runs. Budget-limited rows remain visible instead of being silently discarded.

## Search and indexing

SQLite FTS5 searches only imported organisations and pages already collected by SDI. It is not web search. Search terms are tokenised and quoted before building FTS queries. Page snippets are query-centred, and non-indexable or duplicate pages are excluded.

## Operational acceptance criteria

A branch is not production-ready until all of the following are true:

1. GitHub Actions compilation and offline tests pass.
2. Local SQLite migrations succeed on both a fresh database and a copy of the existing database.
3. A controlled network test validates robots behaviour, byte caps, redirects, DNS peer checks, sitemap indexes and duplicate handling.
4. A recursive run can be interrupted and resumed without losing queued domains or exceeding configured limits.
5. Scout receives source URLs, timestamps, evidence status and confidence for each material field.
6. Founder Panel metrics distinguish datasets imported, pages indexed, candidates discovered, candidates accepted/rejected and domains resumed.
7. A real authorised seed or registered SRA API key is available before claiming broad market coverage.

## Known limitations

- No general web-search provider is connected.
- No active HTML seed is currently configured, so recursive discovery is implemented but dormant in the built-in source catalogue.
- SRA API adapter and authenticated calls remain pending operator registration and API key.
- Real-world crawling and local Founder Panel behaviour still require validation against the user's local database and runtime.
