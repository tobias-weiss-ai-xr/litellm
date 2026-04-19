# LiteLLM Proxy Infrastructure

A self-hosted AI model gateway built on LiteLLM, routing requests across local and cloud providers with automatic fallback, caching, and a real-time monitoring dashboard.

## Architecture

```
                    ┌──────────────┐
                    │  Dashboard   │
                    │  :3000 (web) │
                    └──────┬───────┘
                           │ nginx reverse proxy
              ┌────────────┼────────────┐
              ▼            ▼            ▼
      ┌───────────┐ ┌──────────┐ ┌──────────┐
      │  Stats    │ │  Proxy   │ │  Proxy   │
      │  API :3001│ │  :4000   │ │  Health  │
      │           │ │          │ │  checks  │
      └─────┬─────┘ └────┬─────┘ └──────────┘
            │              │
            │         ┌────┴────┐
            │         ▼         ▼
            │   ┌──────────┐ ┌──────────┐
            ├───►│ Postgres │ │  Redis   │
            │   │  :5432   │ │  :6379   │
            │   └──────────┘ └──────────┘
            │
            ▼
      Docker Engine API
      (container stats)
```

## Services

| Service | Container | Port | Image | Purpose |
|---------|-----------|------|-------|---------|
| **litellm-postgres** | `litellm-postgres` | 5432 (internal) | `postgres:16-alpine` | Spend logs, key rotations, request history |
| **litellm-redis** | `litellm-redis` | 6379 (internal) | `redis:7-alpine` | Response cache with LRU eviction |
| **litellm-proxy** | `litellm-proxy` | **4000** | `litellm-opencode:latest` | Model routing gateway (gunicorn, 2 workers) |
| **dashboard** | `litellm-dashboard` | **3000** | `nginx:alpine` | Static dashboard + reverse proxy to stats/proxy |
| **docker-stats** | `docker-stats-api` | **3001** | `litellm-stats-api:latest` | Aggregates Docker + LiteLLM metrics |
| **db-backup** | `litellm-db-backup` | none (ephemeral) | `postgres:16-alpine` | On-demand `pg_dump` to `./backups/` |

## Quick Start

1. **Create `.env`** in the repo root with your API keys:

```env
LITELLM_MASTER_KEY=sk-your-master-key-here
REDIS_PASSWORD=litellm
ZAI_API_KEY=your-zai-key
SAIA_API_KEY=your-saia-key
SAIA_V2_API_KEY=your-saia-v2-key
GOOGLE_API_KEY=your-google-key
GROQ_API_KEY=your-groq-key
OPENROUTER_API_KEY=your-openrouter-key
```

2. **Start everything:**

```bash
docker compose up -d
```

The proxy takes about 15-20 seconds to boot (Prisma migrations run on startup). Wait until health checks pass.

3. **Verify:**

```bash
# Proxy is healthy
curl http://localhost:4000/health/liveliness

# List available models
curl http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"

# Dashboard loads
curl -s http://localhost:3000/api/all | python -m json.tool
```

4. **Access:**
- Proxy API: `http://localhost:4000`
- Dashboard: `http://localhost:3000`

## Configuration

### `opencode_proxy_config.yaml`

This is the main routing configuration, mounted read-only into the proxy container at `/app/config.yaml`.

**Model list** defines all available deployments. Each entry maps a model name to a specific provider endpoint. Duplicate entries create load-balanced pools (e.g., `glm-5-turbo` has 5 seats via Z.ai).

**Router settings** control fallback behavior:

| Setting | Value | Meaning |
|---------|-------|---------|
| `num_retries` | 2 | Retry failed requests twice |
| `retry_after` | 5s | Wait between retries |
| `allowed_fails` | 2 | Cool down a deployment after 2 failures |
| `cooldown_time` | 30s | Keep a deployment cooled down for 30s |

**LiteLLM settings** at the bottom configure global behavior:

- Response caching via Redis (TTL: 1 hour)
- Request timeout: 600s, read timeout: 300s
- HTTP keep-alive: 300s expiry, 40 max keep-alive connections
- `drop_params: true` silently drops unsupported parameters instead of erroring
- Telemetry disabled

### Environment variables

All secrets live in `.env` at the repo root. The proxy container reads them at startup. No keys are baked into config files or images.

## Model Sources

### Z.ai (GLM models)

Primary provider for GLM family models. Accessed via OpenAI-compatible API at `api.z.ai`. Models include `glm-5-turbo` (5 seats for high availability), `glm-5`, `glm-5.1`, `glm-4.7`, and `glm-4.6V`. The `set_reasoning_content_in_choice: true` flag is set on reasoning models to expose chain-of-thought output.

### SAIA (Academic Cloud)

German academic cloud at `chat-ai.academiccloud.de`. Rate-limited and slow, so all SAIA models get 900s timeouts (1200s for the 397b model). On 429 responses, the router falls through to the next fallback rather than retrying.

### Groq

Free-tier ultra-fast inference. Used as a fallback target for local models. Three models available: `llama-3.3-70b-versatile`, `gemma2-9b-it`, `mixtral-8x7b-32768`.

### OpenRouter

Free models accessed through OpenRouter's aggregation. Four models: `llama-4-maverick`, `deepseek-r1`, `gemini-2.0-flash`, `qwen-2.5-72b`.

### Local models (vLLM / llama.cpp)

Hosted on LAN machines, no VPN needed. Two model families:

- **gemma-4-26b-nvfp4** runs on three backends load-balanced by the proxy: two vLLM instances (at `.27:8000` and `.176:8000`) and one llama.cpp instance on the Docker host via `host.docker.internal:8080`. Tool calling is disabled because vLLM backends don't support it.
- **gemma-4-e2b** runs on a separate llama.cpp instance at `.81:8080`. A smaller, faster variant for quick tasks.

## Services Detail

### litellm-postgres

PostgreSQL 16 tuned for proxy workloads: 1 GB shared buffers, 32 MB work_mem, 200 max connections. Data persists in the `litellm-pgdata` Docker volume. Memory capped at 2 GB.

### litellm-redis

Redis 7 in AOF persistence mode with LRU eviction at 2 GB max memory. Stores cached LLM responses. Memory capped at 3 GB.

### litellm-proxy

Built from `Dockerfile.custom` (Python 3.11-slim, installs LiteLLM with proxy extras, Prisma client, gunicorn). Two gunicorn workers, 4 CPU / 4 GB memory limits. Config mounted at `/app/config.yaml` read-only.

### dashboard

Nginx serving the static `index.html` and proxying API requests. The nginx config (`default.conf`) routes:
- `/api/all` to the stats API
- `/stats/` to the stats API (with path stripping)
- `/proxy/` to the LiteLLM proxy on the Docker host
- Everything else as static files

Memory capped at 128 MB.

### docker-stats

Built from `dashboard/Dockerfile.stats` (Python 3.12-alpine + psycopg2-binary). Runs a threaded HTTP server that fetches container stats from the Docker socket and LiteLLM analytics from the proxy API. Uses a background cache thread (10s TTL) so responses return instantly. Also runs auto-migration on startup: creates the `model_group` index and purges spend logs older than 30 days.

Mounts the Docker socket read-only. Memory capped at 256 MB.

### db-backup

Ephemeral container that runs `pg_dump | gzip` to `./backups/`. Keeps only the 7 most recent backups. Not part of the always-on stack, run manually or via scheduled task.

## Monitoring

### Dashboard (`http://localhost:3000`)

A single-page app that auto-refreshes every 15 seconds. Shows:

- **Top bar**: Proxy health (green/red pulse), model count breakdown, Redis cache status
- **Query distribution**: Horizontal bar chart of the top 15 models by request count, with average latency badges (green under 5s, yellow under 15s, red above)
- **Cache and infrastructure**: Redis version, cache type, TTL, ping/write test results, PostgreSQL status
- **Host containers**: Per-container CPU, memory, and network I/O with color-coded utilization bars
- **Registered models**: Grid of all models grouped by provider (Local/Z.ai/SAIA) with readiness status

### Stats API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/all` | Combined container stats + LiteLLM analytics (single round-trip) |
| `GET /api/docker-stats` | Per-container CPU, memory, network I/O |
| `GET /api/litellm-analytics` | Spend by model, cache health, global spend |

All responses are cached for 10 seconds. The `X-Cache: HIT/MISS` header indicates cache status.

### Health checks

Every service has a Docker healthcheck:

| Service | Check | Interval |
|---------|-------|----------|
| postgres | `pg_isready` | 10s |
| redis | `redis-cli ping` | 10s |
| proxy | `GET /health/liveliness` | 30s (60s start period) |
| dashboard | `wget /api/all` | 15s |
| docker-stats | `GET /api/docker-stats` | 30s |

### Quick healthcheck script

Run `python dashboard/_healthcheck.py` from the host for a one-shot status summary covering all services, model availability, cache config, and resource usage.

## Backups

### Automatic

A Windows Task Scheduler job runs daily at 3:00 AM:

```bash
docker compose run --rm db-backup
```

This dumps the database to `./backups/litellm_<timestamp>.sql.gz` and removes backups older than the 7 most recent.

### Manual

```bash
docker compose run --rm db-backup
```

Check existing backups:

```bash
ls -la backups/
```

Restore from a backup:

```bash
gunzip -c backups/litellm_20250419_030000.sql.gz | docker exec -i litellm-postgres psql -U litellm -d litellm
```

## Troubleshooting

### Cooldown cascades

If a provider starts returning errors, the router cools it down after 2 failures for 30 seconds. During that window all traffic shifts to fallbacks, which can overload them and trigger their own cooldowns. To break the cycle:

```bash
# Check which models are cooled down
docker compose logs litellm-proxy 2>&1 | grep -i cooldown | tail -20

# Restart the proxy to clear all cooldown state
docker compose restart litellm-proxy
```

Prevention: local vLLM models have `supports_tool_calling: false` set explicitly. Without this flag, tool-calling requests would hit the vLLM backend, fail, and trigger cooldown cascades across the entire gemma-4-26b-nvfp4 pool.

### Stale model names

If a provider renames or removes a model, the proxy will return 404 for every request to that model name. Check the proxy's model list to verify what's actually loaded:

```bash
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python -m json.tool
```

Update the model name in `opencode_proxy_config.yaml` and restart the proxy.

### Dashboard unhealthy

The dashboard healthcheck hits `GET /api/all`, which depends on both the stats API and the LiteLLM proxy. If the dashboard container shows unhealthy:

1. Check the stats API directly: `curl http://localhost:3001/api/docker-stats`
2. Check proxy health: `curl http://localhost:4000/health/liveliness`
3. Check nginx logs: `docker compose logs dashboard --tail=50`

### Redis connection failures

If the proxy logs Redis connection errors, verify the redis container is running and the password matches:

```bash
docker compose logs litellm-redis --tail=20
docker exec litellm-redis redis-cli -a $REDIS_PASSWORD ping
```

### High memory usage

Each container has hard memory limits in the compose file. If a service keeps hitting its limit and getting OOM-killed:

- **proxy**: increase the `memory` limit under `deploy.resources.limits` (currently 4 GB)
- **postgres**: increase `shared_buffers` in the postgres command and the memory limit
- **redis**: increase `--maxmemory` in the redis command and the memory limit

## File Reference

| File | Purpose |
|------|---------|
| `docker-compose.yaml` | All service definitions, networking, volumes, health checks |
| `opencode_proxy_config.yaml` | Model list, router fallbacks, LiteLLM settings |
| `Dockerfile.custom` | Proxy image build (Python 3.11 + LiteLLM + Prisma + gunicorn) |
| `dashboard/index.html` | Single-page monitoring dashboard |
| `dashboard/stats.py` | Stats API server (Docker metrics + LiteLLM analytics) |
| `dashboard/default.conf` | Nginx reverse proxy config |
| `dashboard/Dockerfile.stats` | Stats API image build (Python 3.12 + psycopg2) |
| `dashboard/_healthcheck.py` | One-shot healthcheck script for manual diagnostics |
