from __future__ import annotations

from .crawler import validate_public_url
from .discovery import discover_from_seed


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
    """Traverse an approved directory seed internally; never promote firms to new seeds.

    Cross-domain links are verified as candidate websites and retained in the
    persistent candidate frontier. A new directory/source must be approved in
    the Source Registry before it can be crawled as another seed.
    """
    root = validate_public_url(seed_url)
    result = discover_from_seed(
        root,
        db_path,
        source_id=source_id,
        max_candidates=max_candidates_per_seed,
        max_seed_pages=max(1, min(int(max_domains), 30)),
        max_seed_depth=max(0, min(int(max_depth), 4)),
        timeout=timeout,
        delay=delay,
        min_score=min_score,
    )
    errors = result.get("errors", [])
    return {
        "status": "completed_with_errors" if errors else "completed",
        "seed_url": root,
        "max_depth": max(0, min(int(max_depth), 4)),
        "max_domains": max(1, min(int(max_domains), 100)),
        "domains_processed": 1,
        "pages_checked": int(result.get("pages_checked", 0)),
        "candidates_found": int(result.get("candidates_found", 0)),
        "candidates_accepted": int(result.get("candidates_accepted", 0)),
        "candidates_rejected": int(result.get("candidates_rejected", 0)),
        "organisations_created": int(result.get("organisations_created", 0)),
        "organisations_updated": int(result.get("organisations_updated", 0)),
        "queue_remaining": 0,
        "results": [{
            "seed_url": root,
            "pages_checked": result.get("pages_checked", 0),
            "candidates_found": result.get("candidates_found", 0),
            "candidates_accepted": result.get("candidates_accepted", 0),
            "candidates_rejected": result.get("candidates_rejected", 0),
        }],
        "errors": errors,
    }
