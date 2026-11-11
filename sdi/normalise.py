from __future__ import annotations
import re
from urllib.parse import urlparse

def clean(value):
    if value is None: return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None

def normalise_name(value: str) -> str:
    value = clean(value) or ""
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def normalise_url(value: str | None) -> str | None:
    value = clean(value)
    if not value or value.casefold() in {"n/a", "na", "none", "-", "not available"}: return None
    if not value.startswith(("http://", "https://")): value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname: return None
    return value.rstrip("/")

def domain_of(url: str | None) -> str | None:
    if not url: return None
    return (urlparse(url).hostname or "").lower().removeprefix("www.") or None
