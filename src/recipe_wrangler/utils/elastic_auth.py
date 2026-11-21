"""urllib does not apply credentials embedded in a URL (requests does); ELASTIC_URL carries them."""
import base64
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request


def elastic_request(url: str, data: bytes | None = None, method: str | None = None,
                    headers: dict | None = None) -> Request:
    """urllib Request with `user:password@` moved from the URL into a Basic auth header."""
    parts = urlsplit(url)
    headers = dict(headers or {})
    if parts.username:
        token = base64.b64encode(f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()).decode()
        headers["Authorization"] = "Basic " + token
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        url = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return Request(url, data=data, method=method, headers=headers)
