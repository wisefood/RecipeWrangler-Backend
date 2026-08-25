"""Safe, deterministic recipe extraction from a public web page.

Only schema.org ``Recipe`` JSON-LD is accepted. Missing duration or serving
count stays missing; callers decide whether a preview is importable.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

MAX_HTML_BYTES = 2_000_000
MAX_REDIRECTS = 4
USER_AGENT = "RecipeWrangler/1.0 (+recipe import)"


class RecipeUrlError(ValueError):
    pass


def _validate_public_url(url: str) -> str:
    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError as exc:
        raise RecipeUrlError("Invalid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RecipeUrlError("Recipe URL must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise RecipeUrlError("Recipe URL must not contain credentials")
    if port not in {None, 80, 443}:
        raise RecipeUrlError("Recipe URL must use port 80 or 443")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise RecipeUrlError("Private or local recipe URLs are not allowed")
    try:
        addresses = socket.getaddrinfo(hostname, port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise RecipeUrlError("Recipe host could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise RecipeUrlError("Private, local, or reserved recipe URLs are not allowed")
    return raw


def fetch_recipe_html(url: str, *, timeout: float = 10.0) -> tuple[str, str]:
    """Fetch bounded HTML, validating every redirect against SSRF targets."""
    current = _validate_public_url(url)
    session = requests.Session()
    for _ in range(MAX_REDIRECTS + 1):
        response = session.get(
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise RecipeUrlError("Recipe page returned an invalid redirect")
            current = _validate_public_url(urljoin(current, location))
            continue
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "html" not in content_type:
            response.close()
            raise RecipeUrlError("Recipe URL did not return HTML")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_HTML_BYTES:
            response.close()
            raise RecipeUrlError("Recipe page is too large")
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_HTML_BYTES:
                response.close()
                raise RecipeUrlError("Recipe page is too large")
        encoding = response.encoding or "utf-8"
        response.close()
        return body.decode(encoding, errors="replace"), current
    raise RecipeUrlError("Recipe page redirected too many times")


def _recipe_nodes(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _recipe_nodes(item)
    elif isinstance(value, dict):
        raw_type = value.get("@type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if any(str(item).lower() == "recipe" for item in types):
            yield value
        if "@graph" in value:
            yield from _recipe_nodes(value["@graph"])


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


def _minutes(value: Any) -> float | None:
    match = _DURATION_RE.match(str(value or "").strip())
    if not match:
        return None
    parts = {key: float(raw or 0) for key, raw in match.groupdict().items()}
    total = parts["days"] * 1440 + parts["hours"] * 60 + parts["minutes"] + parts["seconds"] / 60
    return total if total > 0 else None


def _serves(value: Any) -> float | None:
    if isinstance(value, (int, float)) and float(value) > 0:
        return float(value)
    text = str(value or "").strip()
    numbers = re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])", text)
    if len(numbers) != 1:
        return None
    result = float(numbers[0])
    return result if result > 0 else None


def _instructions(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("name") or "").strip()
            if text:
                out.append(text)
            out.extend(_instructions(item.get("itemListElement")))
    return list(dict.fromkeys(out))


def _image(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            found = _image(item)
            if found:
                return found
    if isinstance(value, dict):
        return _image(value.get("url") or value.get("contentUrl"))
    return None


def parse_recipe_html(html: str, source_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates.extend(_recipe_nodes(payload))
    if not candidates:
        raise RecipeUrlError("No schema.org Recipe data was found on this page")
    node = max(candidates, key=lambda item: len(item.get("recipeIngredient") or []))
    title = str(node.get("name") or "").strip()
    ingredients = [
        str(item).strip() for item in (node.get("recipeIngredient") or [])
        if str(item).strip()
    ]
    if not title or not ingredients:
        raise RecipeUrlError("Recipe data is missing a title or ingredients")
    total_time = _minutes(node.get("totalTime"))
    if total_time is None:
        prep = _minutes(node.get("prepTime")) or 0
        cook = _minutes(node.get("cookTime")) or 0
        total_time = prep + cook or None
    serves = _serves(node.get("recipeYield"))
    missing = [
        name for name, value in (
            ("duration", total_time),
            ("serves", serves),
            ("instructions", _instructions(node.get("recipeInstructions"))),
        ) if not value
    ]
    return {
        "title": title,
        "ingredients": ingredients,
        "instructions": _instructions(node.get("recipeInstructions")),
        "duration": total_time,
        "serves": serves,
        "image_url": _image(node.get("image")),
        "url": source_url,
        "missing_required_fields": missing,
    }


def fetch_recipe_from_url(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    html, final_url = fetch_recipe_html(url, timeout=timeout)
    return parse_recipe_html(html, final_url)
