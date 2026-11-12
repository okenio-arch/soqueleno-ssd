# SDI discovery and evidence architecture

## Operating model

The SDI is a focused, owned index for UK immigration legal-service provider intelligence. It is not a general-purpose search engine and it does not decide commercial prospect quality. Scout consumes SDI results and performs the separate business-fit evaluation.

The discovery flow is:

1. **Source Registry / seed approval**: every HTML directory seed must be explicitly registered with its URL, source category, terms URL (where available), reviewed terms, crawl permission, and reuse basis. The source must be enabled and have `source_type=html_directory_seed`.
2. **Seed traversal**: the crawler starts at the approved URL and follows only bounded, same-host directory/navigation links. Page count and internal depth are capped per seed.
3. **External candidate extraction**: external links are deduplicated by domain. Social networks, search engines, file downloads, and non-public URLs are excluded.
4. **Policy gate**: domains explicitly blocked, disabled, or awaiting authorisation in the Source Registry are rejected before an HTTP request.
5. **Candidate verification**: a candidate homepage is fetched only after URL safety and robots checks. Relevance requires both immigration-service language and provider/legal-practice signals; title and description receive more weight than incidental body mentions.
6. **Organisation and evidence creation**: accepted candidates create/update organisation records. Each observation retains source ID, source URL, timestamp, field and confidence. Rejected candidates remain auditable.
7. **Persistent frontier**: candidate URLs are stored as `seed_candidate` with `candidate_only` or a rejection state. A candidate organisation website is never automatically promoted into a new seed. A new directory or discovery source requires explicit operator approval.
8. **Website crawling**: imported/approved organisation websites are crawled separately, within same-host limits, robots rules, SSRF protections, redirect validation and response-size caps.
9. **Quality gate and index**: noindex, thin, search, login, archive, tag and category pages are excluded from the search index. Search requires all query terms and uses field weights favouring organisation names, page titles and descriptions.
10. **Change detection**: each new or changed page body is saved as a version with a content hash and `new` / `changed` classification. The API exposes recent changes.

## Important distinctions

- A discovered URL is not automatically fetched.
- A fetched page is not automatically indexable.
- An indexed page is not automatically authoritative evidence.
- An accepted organisation candidate is not automatically a qualified Soqueleno sales prospect.
- A candidate website is not a new discovery seed.
- An imported IAA snapshot is historical evidence, not proof of current authorisation.

## Source activation

No source should be activated merely because it is publicly reachable. The operator must confirm the applicable terms and lawful reuse basis. The Law Society, SRA Data Share API and IAA Adviser Finder remain blocked until their access and reuse requirements are satisfied. No paid web-search provider is integrated.

The API rejects seed creation unless terms review and crawl permission are explicitly confirmed. Seed limits default to 8 pages and depth 2, with hard caps of 30 pages and depth 4.

## Operational API

- `GET /api/v1/sources`: registered sources and their metadata.
- `POST /api/v1/sources`: add an approved HTML seed.
- `PATCH /api/v1/sources/{source_id}/status`: enable/disable a source subject to adapter and policy checks.
- `GET /api/v1/discovery/frontier`: inspect queued, candidate and rejected URLs.
- `GET /api/v1/discovery/changes`: inspect recent page versions.
- `POST /api/v1/discovery/run`: enqueue the integrated discovery cycle.
- `GET /api/v1/search`: search organisations and eligible pages.
- `GET /api/v1/organisations/{id}`: retrieve organisation profile and source-linked evidence.

## Validation strategy

Offline tests cover URL safety, robots/redirect constraints, source policy, approved seed traversal, candidate filtering, page quality, search precision, schema creation and page-version change detection. A separate controlled network test is still required before enabling real seed collection. Start with one approved seed, low page/depth limits, inspect every candidate and source URL, then raise limits only after measuring precision and crawl errors.
