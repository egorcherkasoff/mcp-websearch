#!/usr/bin/env bash
# Create .env from .env.example with freshly generated secrets.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
    echo ".env already exists - leaving it untouched"
    exit 0
fi

cp .env.example .env

secret="$(openssl rand -hex 32)"
token="$(openssl rand -hex 32)"
crawl_token="$(openssl rand -hex 32)"

sed -i "s/^SEARXNG_SECRET=.*/SEARXNG_SECRET=${secret}/" .env
sed -i "s/^MCP_AUTH_TOKEN=.*/MCP_AUTH_TOKEN=${token}/" .env
sed -i "s/^CRAWL4AI_API_TOKEN=.*/CRAWL4AI_API_TOKEN=${crawl_token}/" .env
chmod 600 .env

echo "Created .env with fresh SEARXNG_SECRET, MCP_AUTH_TOKEN and CRAWL4AI_API_TOKEN"
