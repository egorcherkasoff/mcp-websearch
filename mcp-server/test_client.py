#!/usr/bin/env python3
"""Smoke test for the MCP websearch server.

Usage (inside the mcp container):
    docker compose exec mcp python test_client.py

Or from anywhere with the mcp package installed:
    MCP_URL=http://127.0.0.1:8765/mcp MCP_AUTH_TOKEN=... python test_client.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8765/mcp")
TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()


def show(title: str, obj: object) -> None:
    print(f"\n=== {title} ===")
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    print(text if len(text) <= 4000 else text[:4000] + "\n... [truncated]")


async def main() -> int:
    if not TOKEN:
        print(
            "MCP_AUTH_TOKEN is empty: the server refuses unauthenticated requests, "
            "set the token and retry",
            file=sys.stderr,
        )
        return 2
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with streamablehttp_client(MCP_URL, headers=headers) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            show("tools", names)
            assert set(names) >= {"web_search", "scrape_url", "scrape_urls"}, names

            result = await session.call_tool(
                "web_search", {"query": "searxng docker compose", "max_results": 3}
            )
            assert not result.isError, result
            search = json.loads(result.content[0].text)
            show("web_search", search)
            assert search.get("results"), "web_search returned no results"

            first_url = search["results"][0]["url"]
            result = await session.call_tool("scrape_url", {"url": first_url, "max_chars": 1500})
            assert not result.isError, result
            page = json.loads(result.content[0].text)
            show("scrape_url", {**page, "content": (page.get("content") or "")[:800]})
            assert page.get("content"), "scrape_url returned no content"

    print("\nOK: MCP server is working")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
