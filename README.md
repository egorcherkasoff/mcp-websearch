# mcp-websearch — self-hosted web search & scraping as an MCP server

> 🌐 Read this in other languages: **English** | [Русский](README.ru.md)

A private MCP server for web search and page scraping. Runs entirely in Docker,
uses **SearXNG** (metasearch) + **Crawl4AI** (headless browser and content
extraction) and provides OpenCode with three tools:

| Tool           | What it does                                                                     |
| -------------- | -------------------------------------------------------------------------------- |
| `web_search`   | Web search via a private SearXNG (JSON API), returns links and snippets           |
| `scrape_url`   | Fetches a single page via Crawl4AI and returns the main text as Markdown          |
| `scrape_urls`  | Same as above for multiple pages (up to 10) in a single call                      |

The stack is designed to run **only on a LAN or ZeroTier** and must not be
exposed to the internet.

## Architecture

```
OpenCode ──HTTP (Streamable MCP, Bearer token)──▶ mcp-websearch-server:8765/mcp
                                                       │
                                       docker network "websearch"
                                        ┌──────────────┴──────────────┐
                                        ▼                             ▼
                                  searxng:8080                  crawl4ai:11235
                                  (JSON API + web UI)           (REST, headless Chromium)
```

* By default the ports are published on `127.0.0.1` only — the stack is
  reachable from this machine alone. For LAN/ZeroTier access, set `BIND_ADDR`
  in `.env` explicitly (e.g. `0.0.0.0` or a specific ZeroTier address).
* Crawl4AI is reachable only inside the compose network — its port is not
  published.
* The MCP server **refuses to start without `MCP_AUTH_TOKEN`** (fail closed)
  and requires the `Authorization: Bearer <MCP_AUTH_TOKEN>` header on all
  requests except `/healthz`.
* By default `scrape_url`/`scrape_urls` refuse to open loopback, private,
  link-local and cloud-metadata addresses (SSRF protection).

## Quick start

```bash
cd ~/Projects/mcp-websearch
./scripts/init-env.sh        # creates .env with fresh secrets
docker compose up -d --build
docker compose ps            # all services should be healthy
```

Smoke tests:

```bash
# SearXNG JSON API
curl -s 'http://127.0.0.1:8088/search?q=hello&format=json' | head -c 500

# End-to-end test of the MCP server (web_search + scrape_url)
docker compose exec mcp python test_client.py
```

SearXNG web UI: <http://127.0.0.1:8088> (or `http://<host-ip>:8088` from your
LAN / ZeroTier).

## Connecting to OpenCode

```bash
TOKEN=$(grep '^MCP_AUTH_TOKEN=' .env | cut -d= -f2)

opencode mcp add websearch --global \
  --url http://127.0.0.1:8765/mcp \
  --header "Authorization=Bearer $TOKEN"

opencode mcp list            # expect: websearch  connected
```

If OpenCode runs on another machine (e.g. via ZeroTier), replace the address
with that host's IP: `--url http://<zt-ip>:8765/mcp`.

Once connected, the agent sees the `websearch_web_search`,
`websearch_scrape_url`, `websearch_scrape_urls` tools (in Code Mode — as
`tools.websearch.*`).

> The token from this command ends up in `~/.config/opencode/opencode.json`.
> The file is created with user-only permissions; alternatively you can use the
> `{env:MCP_AUTH_TOKEN}` substitution and export the variable into the
> environment OpenCode starts from.

### Making the agent use this MCP specifically

By default OpenCode can browse the web on its own (built-in `websearch` and
`webfetch`), and the agent may prefer them. The global configuration
(`~/.config/opencode/opencode.json`) disables the built-in tools and makes the
MCP tools native (visible to the model directly, not only through Code Mode):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "websearch": false,                      // removes the built-in web search
  "permissions": [
    { "action": "webfetch", "resource": "*", "effect": "deny" }  // deny direct URL reads
  ],
  "mcp": {
    "servers": {
      "websearch": {
        "type": "remote",
        "url": "http://127.0.0.1:8765/mcp",
        "codemode": false,                 // tools in the native list: websearch_web_search, ...
        "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
      }
    }
  }
}
```

Additionally, a global `~/.config/opencode/AGENTS.md` was created instructing
the agent to use `websearch_web_search` / `websearch_scrape_url(s)` for any
internet access. To verify: `opencode run --auto "search the web for ..."`,
the output should show `websearch_web_search`/`websearch_scrape_url` calls.

If you need the built-in `webfetch` back (e.g. the MCP server is down), remove
the `webfetch` rule from `permissions`.

## LAN / ZeroTier access

* By default (`BIND_ADDR=127.0.0.1`) the ports listen on localhost only — the
  safe configuration when OpenCode runs on the same machine.
* For LAN/ZeroTier access, set `BIND_ADDR` in `.env` explicitly:
  * `0.0.0.0` — all interfaces (LAN + ZeroTier + anything else);
  * a specific IP, e.g. the ZeroTier address `192.168.192.10` — then the ports
    listen on that interface only.
* If the host is joined to ZeroTier, other network members can reach the
  services via the host's ZeroTier address (`http://<zt-ip>:8765/mcp`).
* For anything other than `127.0.0.1`, the MCP server logs a warning — make
  sure the token is not compromised.
* On a host with ufw, example access restrictions:

  ```bash
  sudo ufw allow from 192.168.0.0/24 to any port 8765 proto tcp
  sudo ufw allow from 192.168.192.0/24 to any port 8765 proto tcp
  ```

* Ports must **not** be port-forwarded on the router to the internet.

## Configuration

All settings live in `.env`:

| Variable              | Purpose                                                | Default     |
| --------------------- | ------------------------------------------------------ | ----------- |
| `BIND_ADDR`           | bind address for the published ports on the host        | `127.0.0.1` |
| `SEARXNG_PORT`        | host port of the SearXNG web UI/API                     | `8088`      |
| `MCP_PORT`            | host port of the MCP server                             | `8765`      |
| `SEARXNG_SECRET`      | SearXNG secret                                          | —           |
| `MCP_AUTH_TOKEN`      | bearer token of the MCP server (required: the server won't start without it) | — |
| `CRAWL4AI_API_TOKEN`  | bearer token of the Crawl4AI REST API (required: without it the scraper listens on loopback only and is unreachable from the MCP server) | — |
| `MCP_ALLOW_PRIVATE_TARGETS` | allow `scrape_url`/`scrape_urls` to open private/loopback addresses (false — SSRF protection enabled) | `false` |
| `SEARXNG_IMAGE_TAG`   | SearXNG image version                                   | `latest`    |
| `CRAWL4AI_IMAGE_TAG`  | Crawl4AI image version                                  | `latest`    |

SearXNG itself is configured in `searxng/core-config/settings.yml` (e.g. you
can add/disable search engines or drop `json` from `search.formats` to shut off
API access).

MCP server parameters are set via environment variables in
`docker-compose.yml`: `MCP_HTTP_TIMEOUT` (timeout for SearXNG/Crawl4AI
requests, 300 s by default), `MCP_ALLOW_PRIVATE_TARGETS` (`false` by default;
`true` disables SSRF protection and allows scraping internal addresses — use
deliberately).

## Maintenance

```bash
docker compose logs -f mcp            # MCP server logs
docker compose logs -f crawl4ai       # scraper logs
docker compose restart mcp            # restart after a token change
docker compose pull && docker compose up -d   # update images
docker compose down                   # stop (data stays in the volume)
```

Rotating tokens: edit `MCP_AUTH_TOKEN` / `CRAWL4AI_API_TOKEN` in `.env`, run
`docker compose up -d`, and update the OpenCode config (repeat
`opencode mcp add ...` or fix the header in `~/.config/opencode/opencode.json`).

## Security

* **Authentication is mandatory (fail closed).** `MCP_AUTH_TOKEN` is a required
  variable: without it the MCP server refuses to start, and every request
  except `/healthz` requires the `Authorization: Bearer ...` header. To rotate
  the token: edit `.env` and run `docker compose up -d`.
* **Loopback by default.** Ports `8088`/`8765` bind to `127.0.0.1` until a
  different `BIND_ADDR` is set explicitly in `.env`. For LAN/ZeroTier access,
  use a specific interface/address and restrict access with a firewall.
* **SSRF protection.** `scrape_url`/`scrape_urls` validate the scheme
  (http/https only), resolve the host, and reject loopback, private (RFC1918),
  link-local, CGNAT/NAT64, multicast and cloud-metadata addresses (e.g.
  `169.254.169.254`); they also drop content if Crawl4AI followed a redirect to
  such an address. The exception is an explicit `MCP_ALLOW_PRIVATE_TARGETS=true`
  (default `false`). The guard is best-effort: Crawl4AI makes the final request,
  so DNS-rebinding/redirects inside Crawl4AI cannot be fully ruled out — do not
  run the stack on a network with untrusted hosts and do not hand the token to
  untrusted users.
* The Crawl4AI REST API is protected by its own token (`CRAWL4AI_API_TOKEN`)
  and is reachable only from the compose network. SearXNG in this configuration
  has no authentication (as is customary for private instances) — restrict
  access to its UI via network/firewall.
* Secrets are stored in `.env` (chmod 600) and in the OpenCode config.

## Troubleshooting

| Symptom                                   | What to check                                                                   |
| ----------------------------------------- | ------------------------------------------------------------------------------- |
| `docker compose ps` shows `unhealthy`     | `docker compose logs <service>`; crawl4ai takes ~a minute to start               |
| The `mcp` container keeps restarting, logs show `MCP_AUTH_TOKEN is not set` | no token: run `./scripts/init-env.sh` or set `MCP_AUTH_TOKEN` in `.env` and restart (`docker compose up -d`) |
| `scrape_url` returned `refusing to fetch private/internal address` | SSRF protection kicked in; if you deliberately need the internal address, set `MCP_ALLOW_PRIVATE_TARGETS=true` in `.env` |
| OpenCode: `needs authentication`          | the `Authorization` header in the MCP server config, then `opencode mcp list`    |
| `No MCP servers configured` right after editing the config | the OpenCode service re-reads the config; retry the command in a few seconds |
| `401 unauthorized`                        | wrong/stale `MCP_AUTH_TOKEN` (in `.env` and in the OpenCode config)              |
| Search returns empty results              | some SearXNG engines may serve a captcha for the server IP; try `engines="duckduckgo,brave"` or change the engine list in settings.yml |
| `scrape_url` times out                    | heavy pages; increase `MCP_HTTP_TIMEOUT` and `max_chars`                         |
| Port already in use                       | change `SEARXNG_PORT`/`MCP_PORT` in `.env`                                       |
| SearXNG logs `ahmia/torch: can't register engine` | Expected: Tor engines are disabled (no Tor in the stack), harmless        |
| SearXNG logs `X-Forwarded-For nor X-Real-IP header is set!` | Harmless message while the limiter is disabled                   |
