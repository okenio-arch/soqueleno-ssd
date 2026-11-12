from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


def clean(value):
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def normalise_name(value: str) -> str:
    value = clean(value) or ""
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalise_url(value: str | None) -> str | None:
    """Normalise a public website URL without turning contact links into hosts.

    Bare domains are accepted because regulator spreadsheets often omit schemes.
    Explicit non-web schemes (mailto:, tel:, javascript:, etc.) are rejected.
    """
    value = clean(value)
    if not value or value.casefold() in {"n/a", "na", "none", "-", "not available"}:
        return None

    # Reject control characters and whitespace embedded in a URL.
    if any(ord(char) < 32 for char in value) or re.search(r"\s", value):
        return None

    # Preserve explicit web URLs, reject known non-web link schemes, and
    # accept bare domains (including an optional port) from spreadsheets.
    if value.startswith("//"):
        value = "https:" + value
    elif re.match(r"^https?://", value, re.I):
        pass
    elif re.match(r"^(?:mailto|tel|javascript|data|file|ftp|sms):", value, re.I):
        return None
    else:
        value = "https://" + value

    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        # Accessing .port can raise ValueError for malformed values such as
        # https://+442039111115 or https://host:abc.
        port = parsed.port
    except ValueError:
        return None

    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if port not in (None, 80, 443):
        return None

    # Reject malformed host labels, phone numbers and JavaScript fragments.
    host = hostname.rstrip(".").casefold()
    if not host:
        return None
    if ":" not in host and not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*",
        host,
    ):
        return None
    if not re.search(r"[a-z]", host):
        return None

    return value.rstrip("/")


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        # Force validation of malformed ports instead of silently accepting
        # a hostname from a URL such as https://example.com:abc.
        _ = parsed.port
        host = parsed.hostname or ""
    except ValueError:
        return None
    return host.lower().removeprefix("www.") or None


def canonical_page_url(value: str) -> str:
    """Canonicalise page URLs while preserving functional query parameters."""
    parsed = urlparse(value)
    tracking = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref_src"}
    query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
             if key.casefold() not in tracking and not key.casefold().startswith("utm_")]
    query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query, doseq=True), ""))
