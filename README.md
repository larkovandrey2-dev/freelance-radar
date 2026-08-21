# Lead Radar

Personal, low-volume lead scanner. It implements the complete specification: collection, deterministic prefiltering, Yandex Qwen analysis, scoring, Telegram lead alerts, on-demand offers, source expansion, feedback metrics, and a local monitoring panel.

## First start

1. Copy `.env.example` to `.env` and set PostgreSQL and Telegram credentials. Configure the pinned ByeDPI build with `BYEDPI_ARGS`; it must listen on port `1080`.
2. Add channels or groups you already belong to in `config/sources.yaml`.
3. Start dependencies: `docker compose up -d --build postgres byedpi`.
4. Authorize the one persistent Telegram account: `docker compose run --rm radar python -m app.cli telegram-login`.
5. Start the scanner: `docker compose up -d radar`.

The Telegram session lives in the Docker volume at `/data/telegram/radar.session`; it is never committed. PostgreSQL and ByeDPI have no published host ports. The health API is bound only to `127.0.0.1`. `BYEDPI_ARGS` defaults to `--port 1080`; tune it for the target network before enabling a `byedpi` transport.

If local port `8000` is already taken, keep `APP_PORT=8000` and set `HOST_PORT=8001` in `.env`; then use `http://127.0.0.1:8001` for the health API.

## Operations

```bash
docker compose ps
docker compose logs -f radar
curl http://127.0.0.1:8000/health
docker compose run --rm radar python -m app.cli health
docker compose run --rm radar python -m app.cli test-network
docker compose run --rm radar python -m app.cli test-yandex
docker compose run --rm radar python -m app.cli test-notifier
docker compose run --rm radar python -m app.cli sources
```

`test-yandex` makes a small live model request with Yandex's native `reasoningOptions.mode=DISABLED`. It therefore requires a configured API key and may incur a minimal model charge.

## Offers, feedback, and panel

Press `✍️ Отклик` on a Telegram alert to create an offer only for that lead. Price and delivery time are calculated in code from the analysis and configurable floor values; Qwen only phrases the result using `config/profile.yaml`. The generated reply is sent back to the configured Telegram chat.

Feedback actions are stored as durable events and are available alongside sources and leads through the local panel at `http://127.0.0.1:8000/`. Machine-readable endpoints are `/api/dashboard`, `/api/sources`, and `/api/leads`; the panel remains bound to localhost for SSH-tunnel access only.

## Additional sources

Reddit uses low-frequency read-only JSON polling for the explicitly configured subreddits in `config/sources.yaml`. Discord uses the independent `app.sources.discord_listener` process only when `DISCORD_USER_TOKEN`, `AUTH_SECRET_KEY`, and enabled `discord_listener` channel IDs are configured. It forwards normalized packets through the authenticated loopback gateway, which deduplicates them in SQLite before PostgreSQL ingestion. Do not run that collector from a public cloud IP.

## Forum sources

Enabled sources in [`config/sources.yaml`](config/sources.yaml) use public Discourse JSON and fall back to RSS when JSON is unavailable. The initial set is n8n Jobs, n8n Help me Build my Workflow and Make Hire Help. Polling is `45–90` seconds with jitter by default. It uses `ETag`, `If-Modified-Since`, `Retry-After`, bounded retries, and never posts or authenticates to a forum.

## Lead processing and alerts

Every collected message is normalized, scored by an explainable keyword prefilter, then sent to Yandex Qwen only when it is a candidate (or a bounded 1% audit sample). Analyses are cached by SHA-256 of normalized text. Failed model calls stay pending and retry after 5s, 20s, 60s, and then 5m; strong rule matches can alert without AI. Set `LEAD_ALERT_THRESHOLD` (default `72`) to tune delivery.

## Generic local stream gateway

Set `AUTH_SECRET_KEY` and `MY_INTERNAL_ID` in `.env` to enable a local WebSocket listener. It is published only as `127.0.0.1:8765` by default and accepts one token text frame followed by JSON packets. SQLite deduplication is persisted in the `gateway_data` Docker volume; accepted data is then written to `raw_messages` and automatically enters the processing queue.
