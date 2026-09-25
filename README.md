# mcp-websearch — self-hosted веб-поиск и scraping как MCP-сервер

Приватный MCP-сервер для веб-поиска и скрейпинга страниц. Работает целиком в Docker,
использует **SearXNG** (метапоиск) + **Crawl4AI** (headless-браузер и извлечение
контента) и предоставляет OpenCode три инструмента:

| Инструмент    | Что делает                                                                 |
| ------------- | -------------------------------------------------------------------------- |
| `web_search`  | Поиск в интернете через приватный SearXNG (JSON API), возвращает ссылки и сниппеты |
| `scrape_url`  | Загружает одну страницу через Crawl4AI и возвращает основной текст в Markdown |
| `scrape_urls` | То же самое для нескольких страниц (до 10) за один вызов                    |

Стек рассчитан на работу **только в локальной сети или ZeroTier** и не должен
публиковаться в интернет.

## Архитектура

```
OpenCode ──HTTP (Streamable MCP, Bearer token)──▶ mcp-websearch-server:8765/mcp
                                                       │
                                       docker network "websearch"
                                        ┌──────────────┴──────────────┐
                                        ▼                             ▼
                                  searxng:8080                  crawl4ai:11235
                                  (JSON API + web UI)           (REST, headless Chromium)
```

* По умолчанию порты публикуются только на `127.0.0.1` — стек доступен лишь с
  этой машины. Для доступа из LAN/ZeroTier явно задайте `BIND_ADDR` в `.env`
  (например, `0.0.0.0` или конкретный ZeroTier-адрес).
* Crawl4AI доступен только внутри compose-сети — снаружи его порт не публикуется.
* MCP-сервер **не стартует без `MCP_AUTH_TOKEN`** (fail closed) и требует
  заголовок `Authorization: Bearer <MCP_AUTH_TOKEN>` на всех запросах, кроме
  `/healthz`.
* `scrape_url`/`scrape_urls` по умолчанию отказываются открывать loopback,
  приватные, link-local и cloud-metadata адреса (SSRF-защита).

## Быстрый старт

```bash
cd ~/Projects/mcp-websearch
./scripts/init-env.sh        # создаст .env со свежими секретами
docker compose up -d --build
docker compose ps            # все сервисы должны быть healthy
```

Проверка:

```bash
# SearXNG JSON API
curl -s 'http://127.0.0.1:8088/search?q=hello&format=json' | head -c 500

# Сквозной тест MCP-сервера (web_search + scrape_url)
docker compose exec mcp python test_client.py
```

Веб-интерфейс SearXNG: <http://127.0.0.1:8088> (или `http://<IP-хоста>:8088`
из локалки / ZeroTier).

## Подключение к OpenCode

```bash
TOKEN=$(grep '^MCP_AUTH_TOKEN=' .env | cut -d= -f2)

opencode mcp add websearch --global \
  --url http://127.0.0.1:8765/mcp \
  --header "Authorization=Bearer $TOKEN"

opencode mcp list            # ожидаем: websearch  connected
```

Если OpenCode запускается на другой машине (например, через ZeroTier), замените
адрес на IP этого хоста: `--url http://<zt-ip>:8765/mcp`.

После подключения агент увидит инструменты `websearch_web_search`,
`websearch_scrape_url`, `websearch_scrape_urls` (в Code Mode — как
`tools.websearch.*`).

> Токен в этой команде попадает в `~/.config/opencode/opencode.json`. Файл
> создаётся с правами только для пользователя; при желании можно вместо этого
> использовать подстановку `{env:MCP_AUTH_TOKEN}` и экспортировать переменную
> в окружение, из которого стартует OpenCode.

### Чтобы агент использовал именно этот MCP

По умолчанию OpenCode умеет ходить в интернет сам (встроенный `websearch` и
`webfetch`), и агент может предпочесть их. Глобальная конфигурация
(`~/.config/opencode/opencode.json`) отключает встроенные инструменты и делает
инструменты MCP нативными (видимыми модели напрямую, а не только через Code Mode):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "websearch": false,                      // убирает встроенный веб-поиск
  "permissions": [
    { "action": "webfetch", "resource": "*", "effect": "deny" }  // запрет прямого чтения URL
  ],
  "mcp": {
    "servers": {
      "websearch": {
        "type": "remote",
        "url": "http://127.0.0.1:8765/mcp",
        "codemode": false,                 // инструменты в нативном списке: websearch_web_search, ...
        "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
      }
    }
  }
}
```

Дополнительно создан глобальный `~/.config/opencode/AGENTS.md` с инструкцией
использовать `websearch_web_search` / `websearch_scrape_url(s)` для любых
запросов в интернет. Проверка: `opencode run --auto "найди в интернете ..."` —
в выводе должны быть вызовы `websearch_web_search`/`websearch_scrape_url`.

Если нужно вернуть встроенный `webfetch` (например, MCP-сервер недоступен),
удалите правило `webfetch` из `permissions`.

## Доступ по LAN / ZeroTier

* По умолчанию (`BIND_ADDR=127.0.0.1`) порты слушают только localhost — это
  безопасная конфигурация для работы OpenCode на этой же машине.
* Для доступа из LAN/ZeroTier задайте `BIND_ADDR` в `.env` явно:
  * `0.0.0.0` — все интерфейсы (LAN + ZeroTier + любые другие);
  * конкретный IP, например ZeroTier-адрес `192.168.192.10` — тогда порты
    слушают только этот интерфейс.
* Если хост подключён к ZeroTier, другие участники сети видят сервисы по
  ZeroTier-адресу хоста (`http://<zt-ip>:8765/mcp`).
* При любом варианте, кроме `127.0.0.1`, MCP-сервер предупреждает об этом в
  логах — убедитесь, что токен не скомпрометирован.
* На хосте с ufw пример ограничения доступа:

  ```bash
  sudo ufw allow from 192.168.0.0/24 to any port 8765 proto tcp
  sudo ufw allow from 192.168.192.0/24 to any port 8765 proto tcp
  ```

* Порты **не** должны пробрасываться на роутере в интернет.

## Конфигурация

Все настройки — в `.env`:

| Переменная            | Назначение                                             | По умолчанию |
| --------------------- | ------------------------------------------------------ | ------------ |
| `BIND_ADDR`           | адрес привязки портов на хосте                         | `127.0.0.1`  |
| `SEARXNG_PORT`        | порт web UI/API SearXNG на хосте                       | `8088`       |
| `MCP_PORT`            | порт MCP-сервера на хосте                              | `8765`       |
| `SEARXNG_SECRET`      | секрет SearXNG                                         | —            |
| `MCP_AUTH_TOKEN`      | bearer-токен MCP-сервера (обязателен: без него сервер не стартует) | —   |
| `CRAWL4AI_API_TOKEN`  | bearer-токен REST API Crawl4AI (обязателен: без него скрейпер слушает только loopback и недоступен MCP-серверу) | — |
| `MCP_ALLOW_PRIVATE_TARGETS` | разрешить `scrape_url`/`scrape_urls` открывать приватные/loopback-адреса (false — SSRF-защита включена) | `false` |
| `SEARXNG_IMAGE_TAG`   | версия образа SearXNG                                  | `latest`     |
| `CRAWL4AI_IMAGE_TAG`  | версия образа Crawl4AI                                 | `latest`     |

Настройки самого SearXNG — `searxng/core-config/settings.yml` (например, можно
добавить/выключить поисковые движки или убрать `json` из `search.formats`,
чтобы запретить доступ к API).

Параметры MCP-сервера задаются переменными окружения в `docker-compose.yml`:
`MCP_HTTP_TIMEOUT` (таймаут запросов к SearXNG/Crawl4AI, по умолчанию 300 c),
`MCP_ALLOW_PRIVATE_TARGETS` (по умолчанию `false`; `true` отключает SSRF-защиту
и разрешает скрейпить внутренние адреса — используйте осознанно).

## Обслуживание

```bash
docker compose logs -f mcp            # логи MCP-сервера
docker compose logs -f crawl4ai       # логи скрейпера
docker compose restart mcp            # перезапуск после смены токена
docker compose pull && docker compose up -d   # обновление образов
docker compose down                   # остановка (данные остаются в volume)
```

Смена токенов: отредактируйте `MCP_AUTH_TOKEN` / `CRAWL4AI_API_TOKEN` в `.env`,
выполните `docker compose up -d` и обновите конфиг OpenCode (повторите
`opencode mcp add ...` или поправьте заголовок в `~/.config/opencode/opencode.json`).

## Безопасность

* **Аутентификация обязательна (fail closed).** `MCP_AUTH_TOKEN` — обязательная
  переменная: без неё MCP-сервер отказывается стартовать, а все запросы кроме
  `/healthz` требуют заголовок `Authorization: Bearer ...`. Сменить токен:
  отредактировать `.env` и выполнить `docker compose up -d`.
* **Loopback по умолчанию.** Порты `8088`/`8765` привязываются к `127.0.0.1`,
  пока в `.env` явно не задан другой `BIND_ADDR`. Для доступа из LAN/ZeroTier
  используйте конкретный интерфейс/адрес и ограничьте доступ firewall'ом.
* **SSRF-защита.** `scrape_url`/`scrape_urls` проверяют схему (только http/https),
  резолвят хост и отклоняют loopback, приватные (RFC1918), link-local,
  CGNAT/NAT64, multicast и cloud-metadata адреса (например, `169.254.169.254`),
  а также не возвращают контент, если Crawl4AI ушёл редиректом на такой адрес.
  Исключение — явный `MCP_ALLOW_PRIVATE_TARGETS=true` (по умолчанию `false`).
  Ограничение best-effort: окончательный запрос делает Crawl4AI, поэтому
  DNS-rebinding/редиректы внутри Crawl4AI полностью не исключены — не
  запускайте стек в сети с недоверенными хостами и не выдавайте токен
  недоверенным пользователям.
* REST API Crawl4AI защищён собственным токеном (`CRAWL4AI_API_TOKEN`) и доступен
  только из compose-сети. SearXNG в этой конфигурации без аутентификации (как и
  принято для приватных инстансов) — доступ к его UI ограничивайте сетью/firewall.
* Секреты хранятся в `.env` (chmod 600) и в конфиге OpenCode.

## Troubleshooting

| Симптом                                   | Что проверить                                                                 |
| ----------------------------------------- | ----------------------------------------------------------------------------- |
| `docker compose ps` — `unhealthy`         | `docker compose logs <service>`; у crawl4ai старт занимает ~минуту             |
| Контейнер `mcp` перезапускается, в логах `MCP_AUTH_TOKEN is not set` | токен не задан: выполните `./scripts/init-env.sh` или пропишите `MCP_AUTH_TOKEN` в `.env` и перезапустите (`docker compose up -d`) |
| `scrape_url` вернул `refusing to fetch private/internal address` | сработала SSRF-защита; если внутренний адрес нужен осознанно, задайте `MCP_ALLOW_PRIVATE_TARGETS=true` в `.env` |
| OpenCode: `needs authentication`          | заголовок `Authorization` в конфиге MCP-сервера, затем `opencode mcp list`     |
| `No MCP servers configured` сразу после правки конфига | сервис OpenCode перечитывает конфиг; повторите команду через пару секунд |
| `401 unauthorized`                        | неверный/устаревший `MCP_AUTH_TOKEN` (в `.env` и в конфиге OpenCode)           |
| Поиск возвращает пустой результат         | некоторые движки SearXNG могут отдавать капчу с IP сервера; попробуйте `engines="duckduckgo,brave"` или смените список движков в settings.yml |
| `scrape_url` таймаутит                    | тяжёлые страницы; увеличьте `MCP_HTTP_TIMEOUT` и `max_chars`                   |
| Порт занят                                 | поменяйте `SEARXNG_PORT`/`MCP_PORT` в `.env`                                   |
| В логах SearXNG `ahmia/torch: can't register engine` | Ожидаемо: Tor-движки отключены (Tor в стеке нет), на работу не влияет |
| В логах SearXNG `X-Forwarded-For nor X-Real-IP header is set!` | Безвредное сообщение при выключенном limiter                |
