#!/usr/bin/env python3
"""MCP server exposing self-hosted web search (SearXNG) and scraping (Crawl4AI).

Tools:
  * web_search  - query the private SearXNG JSON API
  * scrape_url  - fetch one page with Crawl4AI, return main content as Markdown
  * scrape_urls - batch version of scrape_url (up to 10 pages)

Transport: Streamable HTTP on MCP_HOST:MCP_PORT, endpoint /mcp.
Auth:      optional static bearer token (MCP_AUTH_TOKEN); /healthz stays open.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
CRAWL4AI_URL = os.environ.get("CRAWL4AI_URL", "http://crawl4ai:11235").rstrip("/")
CRAWL4AI_API_TOKEN = os.environ.get("CRAWL4AI_API_TOKEN", "").strip()
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8765"))
HTTP_TIMEOUT = float(os.environ.get("MCP_HTTP_TIMEOUT", "300"))

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
    return [_shape_result(item, include_links) for item in results]


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


def main() -> None:
    app = mcp.streamable_http_app()
    if AUTH_TOKEN:
        app.add_middleware(BearerAuthMiddleware, token=AUTH_TOKEN)
    uvicorn.run(app, host=HOST, port=PORT, log_level=os.environ.get("MCP_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
