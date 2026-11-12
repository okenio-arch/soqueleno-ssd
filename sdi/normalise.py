from __future__ import annotations

import re
from urllib.parse import urlparse


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

    # A scheme is explicit if it follows RFC-style scheme syntax.
    scheme_match = re.match(r"^([a-z][a-z0-9+.-]*):", value, re.I)
    if scheme_match:
        scheme = scheme_match.group(1).casefold()
        if scheme not in {"http", "https"}:
            return None
    elif value.startswith("//"):
        value = "https:" + value
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
        host = urlparse(url).hostname or ""
    except ValueError:
        return None
    return host.lower().removeprefix("www.") or None
