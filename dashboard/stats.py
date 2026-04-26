"""Stats API server for the LiteLLM monitoring dashboard.

Provides a threaded HTTP server that aggregates metrics from:
- Docker Engine API (via Unix socket) for container resource stats
- LiteLLM proxy API (via TCP) for spend, cache health, and usage analytics

Endpoints:
    GET /api/docker-stats   - Per-container CPU, memory, network I/O
    GET /api/litellm-analytics - Spend by model, cache health, global spend
    GET /api/all            - Combined response (single round-trip for dashboard)

Runs as a lightweight Python container with no external dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Try to import psycopg2 for direct DB queries (request counts)
try:
    import psycopg2

    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False
    logger.warning("psycopg2 not available, request counts will fall back to API data")

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
DOCKER_SOCK: str = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
LITELLM_HOST: str = os.environ.get("LITELLM_HOST", "litellm-proxy")
LITELLM_PORT: int = int(os.environ.get("LITELLM_PORT", "4000"))
LITELLM_KEY: str = os.environ.get(
    "LITELLM_MASTER_KEY",
    "sk-b55922061536c9f9c19532d904871f2f9a90894e5df34b6ed73b41150c09b817",
)
LISTEN_PORT: int = int(os.environ.get("STATS_PORT", "8000"))
SOCKET_TIMEOUT: int = 10
REQUEST_TIMEOUT: int = 15
CACHE_TTL: int = int(os.environ.get("STATS_CACHE_TTL", "10"))  # seconds

# ---------------------------------------------------------------------------
# In-memory response cache
# ---------------------------------------------------------------------------


class _ResponseCache:
    """Thread-safe time-based cache for API responses.

    Each endpoint gets its own cached entry.  A background refresh thread
    keeps the cache warm so HTTP handlers return instantly.
    """

    def __init__(self, ttl: int = CACHE_TTL) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, bytes]] = {}  # path -> (ts, json_bytes)
        self._fetchers: dict[str, Any] = {}  # path -> callable
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register(self, path: str, fetcher: Any) -> None:
        """Register a fetcher function for a given API path."""
        self._fetchers[path] = fetcher

    def get(self, path: str) -> Optional[bytes]:
        """Return cached JSON bytes if fresh, else None."""
        with self._lock:
            entry = self._store.get(path)
            if entry and (time.monotonic() - entry[0]) < self._ttl:
                return entry[1]
        return None

    def _refresh(self, path: str) -> None:
        """Refresh the cache for a single path."""
        fetcher = self._fetchers.get(path)
        if not fetcher:
            return
        try:
            data = json.dumps(fetcher()).encode()
            with self._lock:
                self._store[path] = (time.monotonic(), data)
        except Exception as e:
            logger.warning("Cache refresh failed for %s: %s", path, e)

    def _refresh_loop(self) -> None:
        """Background loop that refreshes all registered endpoints."""
        while not self._stop.is_set():
            for path in list(self._fetchers):
                self._refresh(path)
            # Sleep for TTL, but wake every second to check stop flag
            for _ in range(self._ttl):
                if self._stop.is_set():
                    return
                time.sleep(1)

    def start(self) -> None:
        """Start the background refresh thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Initial population (blocking, so first request is fast)
        for path in self._fetchers:
            self._refresh(path)
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()
        logger.info("Response cache started (TTL=%ds, %d endpoints)", self._ttl, len(self._fetchers))

    def stop(self) -> None:
        """Stop the background refresh thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


_cache = _ResponseCache()

# PostgreSQL connection (for direct request count queries)
PG_HOST: str = os.environ.get("PG_HOST", "litellm-postgres")
PG_PORT: int = int(os.environ.get("PG_PORT", "5432"))
PG_DB: str = os.environ.get("PG_DB", "litellm")
PG_USER: str = os.environ.get("PG_USER", "litellm")
PG_PASSWORD: str = os.environ.get("PG_PASSWORD", "litellm")


# ---------------------------------------------------------------------------
# Low-level socket helpers
# ---------------------------------------------------------------------------


def _recv_all(sock: socket.socket, timeout: int = SOCKET_TIMEOUT) -> bytes:
    """Read from socket until EOF or timeout, returning raw bytes."""
    sock.settimeout(timeout)
    chunks: list[bytes] = []
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except socket.timeout:
        logger.debug("Socket read timed out after %ds", timeout)
    return b"".join(chunks)


def _http_get(
    host: str,
    port: int,
    path: str,
    timeout: int = SOCKET_TIMEOUT,
    extra_headers: Optional[dict[str, str]] = None,
) -> Optional[Any]:
    """Send an HTTP GET request over TCP and return parsed JSON body.

    Args:
        host: Target hostname.
        port: Target port.
        path: HTTP path (e.g. "/health").
        timeout: Socket timeout in seconds.
        extra_headers: Additional HTTP headers to send.

    Returns:
        Parsed JSON body as dict/list, or None on any failure.
    """
    headers = f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n"
    if extra_headers:
        for k, v in extra_headers.items():
            headers += f"{k}: {v}\r\n"
    headers += "\r\n"

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.sendall(headers.encode())
        raw = _recv_all(s, timeout)
        return _parse_json_response(raw)
    except OSError as e:
        logger.warning("TCP request to %s:%s failed: %s", host, port, e)
        return None


def _docker_api_get(path: str, timeout: int = SOCKET_TIMEOUT) -> Optional[Any]:
    """Send an HTTP GET to the Docker Engine API via Unix socket.

    Args:
        path: API path (e.g. "/containers/json?all=false").
        timeout: Socket timeout in seconds.

    Returns:
        Parsed JSON body, or None on failure.
    """
    request = f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(DOCKER_SOCK)
        s.sendall(request.encode())
        raw = _recv_all(s, timeout)
        return _parse_json_response(raw)
    except OSError as e:
        logger.warning("Docker API request to %s failed: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# HTTP response parsing
# ---------------------------------------------------------------------------


def _parse_json_response(raw: bytes) -> Optional[Any]:
    """Parse a raw HTTP response, extracting and decoding the JSON body.

    Handles both Content-Length and Transfer-Encoding: chunked responses.

    Args:
        raw: Complete raw HTTP response bytes (headers + body).

    Returns:
        Parsed JSON as dict/list, or None if body is empty or invalid.
    """
    if not raw:
        return None

    # Split headers from body at the first blank line
    parts = raw.split(b"\r\n\r\n", 1)
    if len(parts) < 2:
        return None

    header_block = parts[0]
    body = parts[1]

    # Determine transfer encoding
    is_chunked = False
    for line in header_block.split(b"\r\n"):
        lower = line.lower()
        if lower.startswith(b"transfer-encoding:"):
            is_chunked = b"chunked" in lower

    if is_chunked:
        return _decode_chunked(body)

    # Not chunked — body is the entire payload
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning("Failed to decode JSON body: %s", e)
        return None


def _decode_chunked(body: bytes) -> Optional[Any]:
    """Decode a chunked transfer-encoded HTTP body.

    Format: <hex-size>\r\n<data>\r\n<hex-size>\r\n<data>\r\n...0\r\n\r\n

    Args:
        body: Raw chunked body bytes (after headers).

    Returns:
        Decoded payload as parsed JSON, or None on failure.
    """
    result: list[bytes] = []
    pos = 0
    length = len(body)

    while pos < length:
        # Find end of chunk size line
        cr = body.find(b"\r\n", pos)
        if cr == -1:
            break

        size_line = body[pos:cr]
        try:
            chunk_size = int(size_line, 16)
        except ValueError:
            # Not a valid chunk size — treat rest as raw data
            result.append(body[pos:])
            break

        pos = cr + 2  # skip \r\n after size
        if chunk_size == 0:
            break  # terminal chunk

        end = pos + chunk_size
        if end > length:
            end = length  # incomplete chunk, take what we have
        result.append(body[pos:end])
        pos = end + 2  # skip trailing \r\n

    payload = b"".join(result)
    if not payload:
        return None

    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning("Failed to decode chunked body: %s", e)
        return None


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def fetch_container_stats() -> list[dict[str, Any]]:
    """Fetch CPU, memory, and network stats for all running Docker containers.

    Uses parallel threads for /stats?stream=false requests to avoid
    sequential blocking.

    Returns:
        List of dicts with keys: name, status, image, cpu_percent,
        mem_usage, mem_limit, mem_percent, net_rx, net_tx.
    """
    try:
        from safe_stats import get_container_stats_safely
        return get_container_stats_safely()
    except ImportError:
        logger.warning("safe_stats not available, falling back to Docker socket")
        containers = _docker_api_get("/containers/json?all=false")
        if not containers:
            logger.info("No containers returned from Docker API")
            return []

        results: list[Optional[Any]] = [None] * len(containers)

        def _fetch_one(idx: int, cid: str) -> None:
            results[idx] = _docker_api_get(f"/containers/{cid}/stats?stream=false")

        threads: list[threading.Thread] = []
        for i, c in enumerate(containers):
            if c.get("State") == "running":
                t = threading.Thread(target=_fetch_one, args=(i, c["Id"]))
                t.start()
                threads.append(t)

        for t in threads:
            t.join(timeout=REQUEST_TIMEOUT)

        out: list[dict[str, Any]] = []
        for i, c in enumerate(containers):
            name = c.get("Names", [""])[0].lstrip("/")
            entry: dict[str, Any] = {
                "name": name,
                "status": c.get("State", "unknown"),
                "image": c.get("Image", ""),
                "cpu_percent": 0.0,
                "mem_usage": 0,
                "mem_limit": 0,
                "mem_percent": 0.0,
                "net_rx": 0,
                "net_tx": 0,
            }
            stats = results[i]
            if stats:
                try:
                    cpu_delta = (
                        stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
                    )
                    sys_delta = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
                    online_cpus = stats["cpu_stats"].get("online_cpus", 1)
                    if sys_delta > 0:
                        entry["cpu_percent"] = round((cpu_delta / sys_delta) * online_cpus * 100, 1)

                    mem_stats = stats.get("memory_stats", {})
                    mu = mem_stats.get("usage", 0)
                    ml = mem_stats.get("limit", 0)
                    entry["mem_usage"] = mu
                    entry["mem_limit"] = ml
                    entry["mem_percent"] = round(mu / ml * 100, 1) if ml > 0 else 0.0

                    networks = stats.get("networks", {})
                    for iface_data in networks.values():
                        entry["net_rx"] += iface_data.get("rx_bytes", 0)
                        entry["net_tx"] += iface_data.get("tx_bytes", 0)
                except (KeyError, TypeError, ZeroDivisionError) as e:
                    logger.debug("Failed to parse stats for %s: %s", name, e)
            out.append(entry)
        return out


def _query_request_counts() -> list[dict[str, Any]]:
    """Query LiteLLM database directly for request counts and latency by model.

    The /global/spend/models API only returns spend (always 0 for
    free/self-hosted models). This query gets actual request counts
    and average latency from the SpendLogs table.

    Returns:
        List of dicts with model, requests, total_spend, avg_latency_ms keys,
        sorted by request count descending.
    """
    if not PSYCOPG_AVAILABLE:
        return []

    try:
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            database=PG_DB,
            user=PG_USER,
            password=PG_PASSWORD,
        )
        conn.autocommit = True
        cursor = conn.cursor()
        # Get raw request counts + avg latency from SpendLogs
        cursor.execute(
            "SELECT model, model_group, COUNT(*)::int AS requests, "
            "COALESCE(SUM(spend), 0)::float AS total_spend, "
            'COALESCE(AVG(EXTRACT(EPOCH FROM ("endTime" - "startTime")) * 1000), 0)::float AS avg_latency_ms '
            'FROM "LiteLLM_SpendLogs" '
            "WHERE model IS NOT NULL AND model != '' "
            "AND model NOT LIKE '/%' "  # filter out endpoint paths like /models
            "GROUP BY model, model_group ORDER BY requests DESC"
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        # Merge entries by their model_group (preferred) or cleaned model name
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            raw_model = row[0]
            group = row[1] or ""
            avg_latency = row[4] if len(row) > 4 else 0.0

            # Prefer model_group if available (friendly name like "glm-5-turbo")
            if group and group.strip():
                key = group.strip()
            else:
                # Strip provider prefix (openai/, ollama/, etc.)
                key = raw_model.split("/", 1)[1] if "/" in raw_model else raw_model
                # Clean up gguf filenames -> short model name
                if ".gguf" in key.lower():
                    # e.g. "google_gemma-4-26B-A4B-it-Q4_K_M.gguf" -> "gemma-4-26b"
                    base = key.split(".")[0]
                    # Take first meaningful segment (usually model family + size)
                    parts = base.replace("_", "-").split("-")
                    # Keep first 2-3 parts: "google-gemma-4-26B" -> "gemma-4-26b"
                    cleaned_parts = [
                        p
                        for p in parts
                        if p
                        and p.lower()
                        not in ("google", "q4", "q8", "k", "m", "a4b", "a3b", "it", "q2", "q3", "q5", "q6")
                    ]
                    key = "-".join(cleaned_parts[:3]).lower()

            if key in merged:
                merged[key]["requests"] += row[2]
                merged[key]["total_spend"] += row[3]
                # Weighted average latency by request count
                old_reqs = merged[key]["requests"] - row[2]
                merged[key]["avg_latency_ms"] = (
                    (merged[key]["avg_latency_ms"] * old_reqs + avg_latency * row[2]) / merged[key]["requests"]
                    if merged[key]["requests"] > 0
                    else 0.0
                )
            else:
                merged[key] = {
                    "model": key,
                    "requests": row[2],
                    "total_spend": row[3],
                    "avg_latency_ms": avg_latency,
                }

        return sorted(merged.values(), key=lambda x: x["requests"], reverse=True)
    except Exception as e:
        logger.warning("DB request count query failed: %s", e)
        return []


def fetch_litellm_analytics() -> dict[str, Any]:
    """Fetch spend, cache, and usage analytics from LiteLLM proxy.

    Makes parallel requests to /global/spend/models, /global/spend,
    /cache/ping, and /global/spend/logs.

    Returns:
        Dict with keys: spend_by_model, global_spend, cache_health, spend_logs.
    """
    spend_models = _http_get(
        LITELLM_HOST,
        LITELLM_PORT,
        "/global/spend/models",
        extra_headers={"Authorization": f"Bearer {LITELLM_KEY}"},
    )
    global_spend = _http_get(
        LITELLM_HOST,
        LITELLM_PORT,
        "/global/spend",
        extra_headers={"Authorization": f"Bearer {LITELLM_KEY}"},
    )
    cache = _http_get(
        LITELLM_HOST,
        LITELLM_PORT,
        "/cache/ping",
        extra_headers={"Authorization": f"Bearer {LITELLM_KEY}"},
    )
    spend_logs = _http_get(
        LITELLM_HOST,
        LITELLM_PORT,
        "/global/spend/logs",
        extra_headers={"Authorization": f"Bearer {LITELLM_KEY}"},
    )

    cache_info = cache or {}

    # Use DB query for request counts (more accurate than spend-based API)
    request_counts = _query_request_counts()
    if request_counts:
        spend_models = request_counts
    elif spend_models and isinstance(spend_models, list):
        # Fallback: add requests=0 to API data so frontend still renders
        spend_models = [{**m, "requests": m.get("requests", 0)} for m in spend_models]
    else:
        spend_models = []

    return {
        "spend_by_model": spend_models or [],
        "global_spend": global_spend or {},
        "cache_health": {
            "status": cache_info.get("status", "unknown"),
            "cache_type": cache_info.get("cache_type", "none"),
            "ping": cache_info.get("ping_response", False),
            "set_cache": cache_info.get("set_cache_response", "unknown"),
            "ttl": cache_info.get("litellm_cache_params", ""),
            "redis_version": (cache_info.get("health_check_cache_params") or {}).get("redis_version", ""),
        },
        "spend_logs": spend_logs or [],
    }


def _empty_analytics() -> dict[str, Any]:
    """Return a safe default analytics response when LiteLLM is unreachable."""
    return {
        "spend_by_model": [],
        "global_spend": {},
        "cache_health": {
            "status": "error",
            "cache_type": "none",
            "ping": False,
            "set_cache": "error",
            "ttl": "",
            "redis_version": "",
        },
        "spend_logs": [],
    }


def fetch_combined() -> dict[str, Any]:
    """Fetch all metrics (containers + analytics) in parallel.

    Returns:
        Dict with keys: containers (list), analytics (dict).
    """
    container_result: list[Any] = [None]
    analytics_result: dict[str, Any] = [{}]

    def _containers() -> None:
        container_result[0] = fetch_container_stats()

    def _analytics() -> None:
        analytics_result[0] = fetch_litellm_analytics()

    t1 = threading.Thread(target=_containers)
    t2 = threading.Thread(target=_analytics)
    t1.start()
    t2.start()
    t1.join(timeout=REQUEST_TIMEOUT)
    t2.join(timeout=REQUEST_TIMEOUT)

    return {
        "containers": container_result[0] or [],
        "analytics": analytics_result[0] if analytics_result[0] else _empty_analytics(),
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Request handler for the stats API."""

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        # Return cached response if fresh
        cached = _cache.get(path)
        if cached is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Cache", "HIT")
            self.end_headers()
            self.wfile.write(cached)
            return

        # Cache miss — compute synchronously (shouldn't happen with background refresh)
        routes: dict[str, Any] = {
            "/api/docker-stats": fetch_container_stats,
            "/api/litellm-analytics": fetch_litellm_analytics,
            "/api/all": fetch_combined,
        }

        handler = routes.get(path)
        if handler is None:
            self.send_response(404)
            self.end_headers()
            return

        try:
            data = json.dumps(handler()).encode()
        except (TypeError, ValueError) as e:
            logger.error("Failed to serialize response: %s", e)
            self.send_response(500)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Cache", "MISS")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default request logging."""
        pass


class _ThreadedServer(HTTPServer):
    """Threaded HTTP server that allows concurrent request handling."""

    allow_reuse_address = True


# ---------------------------------------------------------------------------
# DB maintenance (index creation + retention cleanup)
# ---------------------------------------------------------------------------

_RETENTION_DAYS: int = 30


def _auto_migrate() -> None:
    """Create missing indexes and purge old spend logs on startup."""
    if not PSYCOPG_AVAILABLE:
        logger.warning("psycopg2 not available, skipping auto-migrate")
        return

    try:
        conn = psycopg2.connect(
            host=os.environ.get("PG_HOST", "litellm-postgres"),
            port=int(os.environ.get("PG_PORT", "5432")),
            dbname=os.environ.get("PG_DB", "litellm"),
            user=os.environ.get("PG_USER", "litellm"),
            password=os.environ.get("PG_PASSWORD", "litellm"),
        )
        conn.autocommit = True
        cur = conn.cursor()

        # 1. Create model_group index if missing (idempotent)
        cur.execute("""
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'LiteLLM_SpendLogs'
              AND indexname = 'idx_spendlogs_model_group'
        """)
        if not cur.fetchone():
            logger.info("Creating index idx_spendlogs_model_group ...")
            cur.execute('CREATE INDEX CONCURRENTLY idx_spendlogs_model_group ON "LiteLLM_SpendLogs" (model_group)')
            logger.info("Index created.")
        else:
            logger.debug("Index idx_spendlogs_model_group already exists.")

        # 2. Delete spend logs older than RETENTION_DAYS
        cur.execute(
            'DELETE FROM "LiteLLM_SpendLogs" WHERE "startTime" < NOW() - INTERVAL %s',
            (f"{_RETENTION_DAYS} days",),
        )
        deleted = cur.rowcount
        if deleted:
            logger.info("Purged %d spend logs older than %d days.", deleted, _RETENTION_DAYS)

        cur.close()
        conn.close()
    except Exception:
        logger.exception("Auto-migrate failed (non-fatal)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Auto-migrate: add indexes and clean up old data on startup
    if os.environ.get("AUTO_MIGRATE", "").lower() in ("true", "1", "yes"):
        _auto_migrate()

    # Register endpoints for background cache refresh
    _cache.register("/api/docker-stats", fetch_container_stats)
    _cache.register("/api/litellm-analytics", fetch_litellm_analytics)
    _cache.register("/api/all", fetch_combined)
    _cache.register("/api/health", fetch_litellm_analytics)
    _cache.start()

    server = _ThreadedServer(("0.0.0.0", LISTEN_PORT), _Handler)
    logger.info("Stats API server listening on :%d", LISTEN_PORT)
    try:
        server.serve_forever()
    finally:
        _cache.stop()
