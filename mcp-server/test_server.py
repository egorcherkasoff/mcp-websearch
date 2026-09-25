#!/usr/bin/env python3
"""Unit tests for the fail-closed bearer auth and the SSRF guard.

Usage (inside the mcp container):
    docker compose exec mcp python -m unittest test_server

Or from anywhere with the requirements installed:
    python -m unittest test_server
"""

from __future__ import annotations

import ipaddress
import os
import unittest
from unittest import mock

import server


class BlockedIpTests(unittest.TestCase):
    def test_private_and_special_ranges_are_blocked(self) -> None:
        blocked = [
            "0.0.0.0",
            "0.1.2.3",
            "10.0.0.1",
            "100.64.0.1",
            "127.0.0.1",
            "169.254.169.254",  # cloud metadata
            "172.16.0.1",
            "192.168.1.1",
            "198.18.0.1",
            "224.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "::ffff:192.168.0.1",  # IPv4-mapped IPv6
            "64:ff9b::a00:1",  # NAT64-wrapped 10.0.0.1
            "100::1",
            "2001::1",  # Teredo
            "2001:db8::1",
            "2002:7f00:1::",  # 6to4-wrapped 127.0.0.1
            "fc00::1",
            "fe80::1",
            "ff02::1",
        ]
        for raw in blocked:
            with self.subTest(ip=raw):
                self.assertTrue(server._is_blocked_ip(ipaddress.ip_address(raw)))

    def test_public_addresses_are_allowed(self) -> None:
        allowed = ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"]
        for raw in allowed:
            with self.subTest(ip=raw):
                self.assertFalse(server._is_blocked_ip(ipaddress.ip_address(raw)))


class AssertPublicTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_literals_are_rejected(self) -> None:
        urls = [
            "http://127.0.0.1/",
            "http://127.0.0.1:8080/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://[::1]/",
            "http://[fe80::1]/",
            "http://[::ffff:192.168.0.1]/",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                await server._assert_public_target(url)

    async def test_localhost_is_rejected_after_dns_resolution(self) -> None:
        with self.assertRaises(ValueError):
            await server._assert_public_target("http://localhost:8080/")

    async def test_public_literals_are_allowed(self) -> None:
        await server._assert_public_target("http://1.1.1.1/")
        await server._assert_public_target("https://[2606:4700:4700::1111]/")

    async def test_bad_scheme_and_missing_host_are_rejected(self) -> None:
        for url in ("ftp://1.1.1.1/", "file:///etc/passwd", "http:///path"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await server._assert_public_target(url)

    async def test_unresolvable_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await server._assert_public_target("http://no-such-host.invalid/")

    async def test_explicit_override_allows_private_targets(self) -> None:
        with mock.patch.object(server, "ALLOW_PRIVATE_TARGETS", True):
            await server._assert_public_target("http://127.0.0.1/")


class RedirectGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_to_private_target_is_dropped(self) -> None:
        result = {
            "url": "http://1.1.1.1/",
            "final_url": "http://127.0.0.1/admin",
            "success": True,
            "content": "internal secret",
        }
        await server._block_private_redirect(result)
        self.assertFalse(result["success"])
        self.assertEqual(result["content"], "")
        self.assertIn("blocked redirect", result["error"])

    async def test_public_redirect_and_same_url_are_untouched(self) -> None:
        public_redirect = {
            "url": "http://1.1.1.1/",
            "final_url": "https://1.0.0.1/",
            "success": True,
            "content": "ok",
        }
        await server._block_private_redirect(public_redirect)
        self.assertTrue(public_redirect["success"])
        self.assertEqual(public_redirect["content"], "ok")

        same_url = {"url": "http://1.1.1.1/", "final_url": "http://1.1.1.1/", "success": True}
        await server._block_private_redirect(same_url)
        self.assertTrue(same_url["success"])


class AuthTests(unittest.TestCase):
    def test_require_auth_token_fails_closed(self) -> None:
        for value in ("", "   "):
            with mock.patch.dict(os.environ, {"MCP_AUTH_TOKEN": value}):
                with self.assertRaises(RuntimeError):
                    server._require_auth_token()

    def test_require_auth_token_returns_configured_token(self) -> None:
        with mock.patch.dict(os.environ, {"MCP_AUTH_TOKEN": "secret-token"}):
            self.assertEqual(server._require_auth_token(), "secret-token")

    def test_middleware_rejects_empty_token(self) -> None:
        with self.assertRaises(ValueError):
            server.BearerAuthMiddleware(app=None, token="")

    def test_middleware_rejects_whitespace_only_token(self) -> None:
        with self.assertRaises(ValueError):
            server.BearerAuthMiddleware(app=None, token=" ")


class _InnerApp:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        self.called = True
        await server._send_json(send, 200, {"inner": True})


class BearerMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _call(
        self, path: str, authorization: str | None = None
    ) -> tuple[int, bool]:
        inner = _InnerApp()
        middleware = server.BearerAuthMiddleware(inner, token="secret")
        scope = {"type": "http", "path": path, "headers": []}
        if authorization is not None:
            scope["headers"].append((b"authorization", authorization.encode()))

        messages: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        return status, inner.called

    async def test_missing_token_gets_401(self) -> None:
        status, called = await self._call("/mcp")
        self.assertEqual(status, 401)
        self.assertFalse(called)

    async def test_wrong_token_gets_401(self) -> None:
        status, called = await self._call("/mcp", "Bearer nope")
        self.assertEqual(status, 401)
        self.assertFalse(called)

    async def test_correct_token_reaches_the_app(self) -> None:
        status, called = await self._call("/mcp", "Bearer secret")
        self.assertEqual(status, 200)
        self.assertTrue(called)

    async def test_healthz_stays_open(self) -> None:
        status, called = await self._call("/healthz")
        self.assertEqual(status, 200)
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
