"""Safe container stats provider without Docker socket access.

Provides limited container statistics using shell commands instead of the Docker API.
This avoids the security risk of mounting the Docker socket directly.

Returns only basic stats that don't require full API access.
"""

from __future__ import annotations

import subprocess
import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_container_stats_safely() -> list[dict[str, Any]]:
    """Get container stats using docker CLI commands instead of socket.

    Returns:
        List of dicts with basic container info: name, status, cpu, memory.
    """
    stats = []

    try:
        # Get container list with basic stats
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line:
                    import json
                    try:
                        data = json.loads(line)
                        stats.append({
                            "name": data.get("Name", "").lstrip("/"),
                            "status": data.get("State", "unknown"),
                            "cpu_percent": float(data.get("CPUPerc", "0%").rstrip('%')),
                            "mem_usage": _parse_bytes(data.get("MemUsage", "0B / 0B").split('/')[0]),
                            "mem_limit": _parse_bytes(data.get("MemUsage", "0B / 0B").split('/')[1]),
                            "mem_percent": float(data.get("MemPerc", "0%").rstrip('%')),
                        })
                    except (json.JSONDecodeError, ValueError, IndexError) as e:
                        logger.warning("Failed to parse container stats: %s", e)

    except subprocess.TimeoutExpired:
        logger.warning("Docker stats command timed out")
    except FileNotFoundError:
        logger.warning("Docker CLI not found")
    except Exception as e:
        logger.warning("Failed to get container stats: %s", e)

    return stats


def _parse_bytes(value: str) -> int:
    """Parse byte values like '1.5GB' to integer bytes."""
    value = value.strip().upper()
    for unit, multiplier in [
        ("TB", 1024**4),
        ("GB", 1024**3),
        ("MB", 1024**2),
        ("KB", 1024**1),
        ("B", 1),
    ]:
        if value.endswith(unit):
            return int(float(value[:-len(unit)]) * multiplier)
    return 0