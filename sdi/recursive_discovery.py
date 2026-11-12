from __future__ import annotations

from collections import deque
from urllib.parse import urlparse

from .db import connect
from .discovery import discover_from_seed
from .crawler import validate_public_url
from .normalise import domain_of


def discover_recursive_from_seed(
    seed_url: str,
    db_path: str,
    *,
    source_id: str,
    max_depth: int = 2,
    max_domains: int = 30,
    max_candidates_per_seed: int = 15,
    timeout: int = 12,
    delay: float = 1.0,
    min_score: float = 0.28,
) -> dict:
    """Run bounded, breadth-first discovery across linked public domains.

    Each accepted organisation website becomes a possible next seed. The
    existing one-hop discovery routine remains responsible for fetching,
    scoring and recording evidence. discovered_urls persists queue state and
    parent provenance for inspection by the console.
    """
    root = validate_public_url(seed_url)
    max_depth = max(0, min(int(max_depth), 5))
    max_domains = max(1, min(int(max_domains), 100))
    max_candidates_per_seed = max(1, min(int(max_candidates_per_seed), 50))

    queue = deque([(root, 0, None, "")])
    queued_domains = {domain_of(root)}
    processed_domains: set[str] = set()
    report = {
        "status": "completed",
        "seed_url": root,
        "max_depth": max_depth,
        "max_domains": max_domains,
        "domains_processed": 0,
        "pages_checked": 0,
        "candidates_found": 0,
        "candidates_accepted": 0,
        "candidates_rejected": 0,
        "organisations_created": 0,
        "organisations_updated": 0,
        "queue_remaining": 0,
        "results": [],
        "errors": [],
    }

    while queue and len(processed_domains) < max_domains:
        current_url, depth, parent_url, anchor_text = queue.popleft()
        current_domain = domain_of(current_url)
        if not current_domain or current_domain in processed_domains:
            continue
        processed_domains.add(current_domain)

        if parent_url:
            with connect(db_path) as conn:
                conn.execute(
                    "UPDATE discovered_urls SET status='processing' WHERE discovered_url=?",
                    (current_url,),
                )

        try:
            result = discover_from_seed(
                current_url,
                db_path,
                source_id=source_id,
                max_candidates=max_candidates_per_seed,
                timeout=timeout,
                delay=delay,
                min_score=min_score,
            )
            report["pages_checked"] += int(result.get("pages_checked", 0))
            report["candidates_found"] += int(result.get("candidates_found", 0))
            report["candidates_accepted"] += int(result.get("candidates_accepted", 0))
            report["candidates_rejected"] += int(result.get("candidates_rejected", 0))
            report["organisations_created"] += int(result.get("organisations_created", 0))
            report["organisations_updated"] += int(result.get("organisations_updated", 0))
            report["results"].append({
                "seed_url": current_url,
                "depth": depth,
                "run_id": result.get("run_id"),
                "pages_checked": result.get("pages_checked", 0),
                "candidates_found": result.get("candidates_found", 0),
                "candidates_accepted": result.get("candidates_accepted", 0),
                "candidates_rejected": result.get("candidates_rejected", 0),
                "errors": result.get("errors", []),
            })
            report["errors"].extend(
                {"seed_url": current_url, **item}
                for item in result.get("errors", [])
            )

            run_id = result.get("run_id")
            if run_id is not None:
                with connect(db_path) as conn:
                    candidates = conn.execute(
                        """SELECT candidate_url,domain,anchor_text,decision
                        FROM organisation_candidates WHERE run_id=?""",
                        (run_id,),
                    ).fetchall()
            else:
                candidates = []

            for candidate in candidates:
                candidate_url = candidate["candidate_url"]
                candidate_domain = candidate["domain"]
                if candidate["decision"] != "verified_candidate":
                    continue
                if not candidate_domain or candidate_domain in queued_domains or candidate_domain in processed_domains:
                    continue

                can_expand = depth < max_depth
                has_capacity = len(processed_domains) + len(queue) < max_domains
                next_status = "queued" if can_expand and has_capacity else (
                    "depth_limited" if not can_expand else "budget_limited"
                )
                with connect(db_path) as conn:
                    conn.execute(
                        """INSERT INTO discovered_urls
                        (parent_url,discovered_url,domain,discovery_type,status,anchor_text)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(discovered_url) DO UPDATE SET
                            parent_url=excluded.parent_url,
                            domain=excluded.domain,
                            discovery_type=excluded.discovery_type,
                            status=CASE
                                WHEN discovered_urls.status IN ('candidate_only','failed','processed','depth_limited','budget_limited')
                                THEN excluded.status
                                ELSE discovered_urls.status
                            END,
                            anchor_text=excluded.anchor_text""",
                        (current_url, candidate_url, candidate_domain, "recursive_seed", next_status,
                         (candidate["anchor_text"] or "")[:300]),
                    )

                if next_status == "queued":
                    queued_domains.add(candidate_domain)
                    queue.append((candidate_url, depth + 1, current_url, candidate["anchor_text"] or ""))

        except Exception as exc:
            report["errors"].append({
                "seed_url": current_url,
                "error": str(exc)[:300],
            })
            if parent_url:
                with connect(db_path) as conn:
                    conn.execute(
                        "UPDATE discovered_urls SET status='failed' WHERE discovered_url=?",
                        (current_url,),
                    )
        else:
            if parent_url:
                final_status = "failed" if int(result.get("pages_checked", 0)) == 0 else "processed"
                with connect(db_path) as conn:
                    conn.execute(
                        "UPDATE discovered_urls SET status=? WHERE discovered_url=?",
                        (final_status, current_url),
                    )

    report["domains_processed"] = len(processed_domains)
    report["queue_remaining"] = len(queue)
    if queue:
        report["status"] = "completed_with_limits"
        report["errors"].append({
            "limit": "max_domains",
            "message": "O limite global de domínios foi atingido; os restantes permanecem na fila.",
        })
    elif report["errors"]:
        report["status"] = "completed_with_errors"
    return report
