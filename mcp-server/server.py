#!/usr/bin/env python3
"""MCP server exposing self-hosted web search (SearXNG) and scraping (Crawl4AI).

Tools:
  * web_search  - query the private SearXNG JSON API
  * scrape_url  - fetch one page with Crawl4AI, return main content as Markdown
  * scrape_urls - batch version of scrape_url (up to 10 pages)

Transport: Streamable HTTP on MCP_HOST:MCP_PORT, endpoint /mcp.
Auth:      MCP_AUTH_TOKEN is REQUIRED. The server refuses to start without it and
           every request except /healthz must carry "Authorization: Bearer ...".
SSRF:      scrape_url/scrape_urls refuse loopback/private/link-local/metadata
           targets unless MCP_ALLOW_PRIVATE_TARGETS=true is set explicitly.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import socket
import urllib.parse
from typing import Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
CRAWL4AI_URL = os.environ.get("CRAWL4AI_URL", "http://crawl4ai:11235").rstrip("/")
CRAWL4AI_API_TOKEN = os.environ.get("CRAWL4AI_API_TOKEN", "").strip()
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8765"))
HTTP_TIMEOUT = float(os.environ.get("MCP_HTTP_TIMEOUT", "300"))
ALLOW_PRIVATE_TARGETS = os.environ.get("MCP_ALLOW_PRIVATE_TARGETS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

MAX_RESULTS_LIMIT = 20
MAX_SCRAPE_URLS = 10
SNIPPET_CHARS = 500

mcp = FastMCP(
    "websearch",
    instructions=(
        "Private web tools backed by a self-hosted SearXNG metasearch instance and "
        "a Crawl4AI scraper. Typical flow: call web_search to find candidate URLs, "
        "then call scrape_url (or scrape_urls) on the most relevant results to read "
        "the full page content."
    ),
    host=HOST,
    port=PORT,
    stateless_http=True,
    json_response=True,
)


# --------------------------------------------------------------------------- #
# SearXNG
# --------------------------------------------------------------------------- #

async def _searxng_json(params: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(f"{SEARXNG_URL}/search", params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"SearXNG returned HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"SearXNG is unreachable at {SEARXNG_URL}: {exc}") from exc


@mcp.tool()
async def web_search(
    query: str,
    max_results: int = 8,
    categories: str | None = None,
    language: str | None = None,
    time_range: str | None = None,
    engines: str | None = None,
    pageno: int = 1,
) -> dict[str, Any]:
    """Search the web through the private SearXNG instance.

    Args:
        query: Search query. SearXNG syntax works, e.g. "site:docs.docker.com compose healthcheck"
            or "!wp docker" to pick engines/categories inline.
        max_results: Maximum number of results to return (1-20).
        categories: Comma-separated SearXNG categories, e.g. "general", "news", "it", "science".
        language: Language code such as "ru", "en", "en-US".
        time_range: Restrict results to "day", "week", "month" or "year".
        engines: Comma-separated engine names, e.g. "duckduckgo,brave,wikipedia".
        pageno: Result page number, starting at 1.
    """
    max_results = max(1, min(int(max_results), MAX_RESULTS_LIMIT))
    params: dict[str, Any] = {
        "q": query,
        "format": "json",
        "pageno": max(1, int(pageno)),
        "safesearch": 0,
    }
    if categories:
        params["categories"] = categories
    if language:
        params["language"] = language
    if time_range:
        params["time_range"] = time_range
    if engines:
        params["engines"] = engines

    data = await _searxng_json(params)

    results: list[dict[str, Any]] = []
    for item in (data.get("results") or [])[:max_results]:
        engines = item.get("engines") or ([item["engine"]] if item.get("engine") else [])
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": (item.get("content") or "").strip()[:SNIPPET_CHARS],
                "engines": engines,
                "publishedDate": item.get("publishedDate"),
                "score": item.get("score"),
            }
        )

    out: dict[str, Any] = {"query": query, "results": results}
    answers = data.get("answers") or []
    if answers:
        out["answers"] = answers
    infoboxes = data.get("infoboxes") or []
    if infoboxes:
        out["infoboxes"] = [
            {"title": box.get("infobox"), "content": (box.get("content") or "")[:SNIPPET_CHARS]}
            for box in infoboxes[:3]
        ]
    return out


# --------------------------------------------------------------------------- #
# SSRF guard for scrape_url / scrape_urls
# --------------------------------------------------------------------------- #

# Ranges that must never be reachable through the scrape tools: loopback,
# private/CGNAT, link-local (incl. cloud metadata 169.254.169.254), multicast,
# reserved/documentation and IPv6 equivalents.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "64:ff9b::/96",
        "100::/64",
        "2001::/32",
        "2001:db8::/32",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when `ip` belongs to a non-public range (IPv4-mapped v6 unwrapped)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in BLOCKED_NETWORKS)


def _parse_target(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("url must start with http:// or https://")
    host = parsed.hostname
    if not host:
        raise ValueError(f"url has no host: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


async def _resolve_host(
    host: str, port: int
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve host {host!r}: {exc}") from exc
    addresses = set()
    for *_, sockaddr in infos:
        try:
            addresses.add(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addresses:
        raise ValueError(f"host {host!r} did not resolve to any usable address")
    return sorted(addresses, key=lambda ip: (ip.version, int(ip)))


async def _assert_public_target(url: str) -> None:
    """Fail closed when `url` points at a non-public address.

    Every address the host resolves to is checked. Crawl4AI resolves the name
    again when it fetches, so a DNS-rebinding attacker could still race this
    check; the guard raises the bar but is not a network-level guarantee.
    """
    if ALLOW_PRIVATE_TARGETS:
        return
    host, port = _parse_target(url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = await _resolve_host(host, port)
    else:
        addresses = [literal]
    blocked = sorted(str(ip) for ip in addresses if _is_blocked_ip(ip))
    if blocked:
        raise ValueError(
            f"refusing to fetch private/internal address {', '.join(blocked)} "
            f"(host {host!r}); set MCP_ALLOW_PRIVATE_TARGETS=true to override"
        )


async def _block_private_redirect(result: dict[str, Any]) -> None:
    """Drop the body of a result whose final URL (after redirects) is non-public."""
    if ALLOW_PRIVATE_TARGETS:
        return
    final_url = result.get("final_url")
    if not final_url or final_url == result.get("url"):
        return
    try:
        await _assert_public_target(final_url)
    except ValueError as exc:
        result["success"] = False
        result["content"] = ""
        result["error"] = f"blocked redirect to non-public target: {exc}"


# --------------------------------------------------------------------------- #
# Crawl4AI
# --------------------------------------------------------------------------- #

def _shape_result(item: dict[str, Any], include_links: bool) -> dict[str, Any]:
    markdown = item.get("markdown") or {}
    content = markdown.get("fit_markdown") or markdown.get("raw_markdown") or ""
    metadata = item.get("metadata") or {}

    shaped: dict[str, Any] = {
        "url": item.get("url"),
        "final_url": item.get("redirected_url") or item.get("url"),
        "status_code": item.get("status_code"),
        "success": item.get("success", True),
        "title": metadata.get("title"),
        "description": metadata.get("description"),
        "language": metadata.get("language"),
        "content": content,
    }
    if item.get("error_message"):
        shaped["error"] = item["error_message"]
    if include_links:
        links = item.get("links") or {}
        shaped["links"] = {
            "internal": [link.get("href") for link in (links.get("internal") or [])][:100],
            "external": [link.get("href") for link in (links.get("external") or [])][:100],
        }
    return shaped


def _truncate(shaped: dict[str, Any], max_chars: int) -> dict[str, Any]:
    content = shaped.get("content") or ""
    if len(content) > max_chars:
        shaped["content"] = content[:max_chars]
        shaped["content_truncated"] = True
        shaped["content_total_chars"] = len(content)
    return shaped


async def _crawl4ai(urls: list[str], include_links: bool) -> list[dict[str, Any]]:
    payload = {
        "urls": urls,
        "browser_config": {
            "type": "BrowserConfig",
            "params": {"headless": True},
        },
        "crawler_config": {
            "type": "CrawlerRunConfig",
            "params": {
                "cache_mode": "bypass",
                "markdown_generator": {
                    "type": "DefaultMarkdownGenerator",
                    "params": {
                        "content_filter": {
                            "type": "PruningContentFilter",
                            "params": {"threshold": 0.45, "threshold_type": "fixed"},
                        },
                        "options": {
                            "ignore_links": not include_links,
                            "ignore_images": True,
                        },
                    },
                },
            },
        },
    }
    headers = (
        {"Authorization": f"Bearer {CRAWL4AI_API_TOKEN}"} if CRAWL4AI_API_TOKEN else {}
    )
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(f"{CRAWL4AI_URL}/crawl", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Crawl4AI returned HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Crawl4AI is unreachable at {CRAWL4AI_URL}: {exc}") from exc

    if not data.get("success"):
        raise RuntimeError(f"Crawl4AI crawl failed: {str(data)[:300]}")

    results = data.get("results") or []
    if not results:
        raise RuntimeError("Crawl4AI returned no results")
    shaped = [_shape_result(item, include_links) for item in results]
    await asyncio.gather(*(_block_private_redirect(item) for item in shaped))
    return shaped


@mcp.tool()
async def scrape_url(
    url: str,
    max_chars: int = 20000,
    include_links: bool = False,
) -> dict[str, Any]:
    """Fetch one web page with Crawl4AI and return its main content as Markdown.

    Use it after web_search to read a specific result. JavaScript-rendered pages
    are supported (a headless browser is used under the hood).

    Args:
        url: Absolute http(s) URL of the page.
        max_chars: Maximum number of Markdown characters to return.
        include_links: Also return the page's internal and external links.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    await _assert_public_target(url)
    max_chars = max(500, int(max_chars))
    results = await _crawl4ai([url], include_links)
    return _truncate(results[0], max_chars)


@mcp.tool()
async def scrape_urls(
    urls: list[str],
    max_chars: int = 12000,
    include_links: bool = False,
) -> dict[str, Any]:
    """Fetch several web pages in one Crawl4AI batch and return their content as Markdown.

    Args:
        urls: Absolute http(s) URLs (up to 10).
        max_chars: Maximum number of Markdown characters returned per page.
        include_links: Also return internal/external links of each page.
    """
    cleaned = [url for url in urls if url.startswith(("http://", "https://"))]
    if not cleaned:
        raise ValueError("no valid http(s) URLs provided")
    if len(cleaned) > MAX_SCRAPE_URLS:
        raise ValueError(f"too many URLs: {len(cleaned)} (max {MAX_SCRAPE_URLS})")
    await asyncio.gather(*(_assert_public_target(url) for url in cleaned))
    max_chars = max(500, int(max_chars))
    results = await _crawl4ai(cleaned, include_links)
    return {"pages": [_truncate(item, max_chars) for item in results]}


# --------------------------------------------------------------------------- #
# HTTP server with static bearer auth
# --------------------------------------------------------------------------- #

async def _send_json(send: Any, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Pure-ASGI bearer-token check. /healthz stays open for container healthchecks."""

    def __init__(self, app: Any, token: str) -> None:
        if not token or not token.strip():
            raise ValueError("BearerAuthMiddleware requires a non-empty token")
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/healthz":
            await _send_json(send, 200, {"status": "ok"})
            return
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        provided = headers.get(b"authorization", b"").decode("latin-1")
        if not hmac.compare_digest(provided, f"Bearer {self.token}"):
            await _send_json(send, 401, {"error": "unauthorized"})
            return
        await self.app(scope, receive, send)


def _require_auth_token() -> str:
    """Return the configured bearer token or fail closed (CWE-306)."""
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "MCP_AUTH_TOKEN is not set: refusing to start without authentication. "
            "Generate one with `openssl rand -hex 32`, put it in .env / the "
            "environment, and restart."
        )
    return token


def main() -> None:
    try:
        token = _require_auth_token()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    logger = logging.getLogger("uvicorn.error")
    if HOST not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(
            "MCP server is binding %s:%s: reachable from the network. "
            "Keep MCP_AUTH_TOKEN secret and restrict access with a firewall.",
            HOST,
            PORT,
        )

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token=token)
    uvicorn.run(app, host=HOST, port=PORT, log_level=os.environ.get("MCP_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
