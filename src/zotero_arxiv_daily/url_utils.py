from html import unescape
from urllib.parse import urlsplit


def safe_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = unescape(value).strip()
    if not url or any(ord(char) < 32 or char.isspace() for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        _ = parsed.port
    except ValueError:
        return None
    return url
