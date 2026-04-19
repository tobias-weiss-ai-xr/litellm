"""Comprehensive pytest unit tests for stats.py."""

import io
import json
import socket
import sys
from unittest.mock import MagicMock, Mock, patch

import pytest

# Import all functions from stats module
from stats import (
    _Handler,
    _decode_chunked,
    _docker_api_get,
    _empty_analytics,
    _http_get,
    _parse_json_response,
    _query_request_counts,
    _recv_all,
    _ThreadedServer,
    fetch_combined,
    fetch_container_stats,
    fetch_litellm_analytics,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_socket():
    """Create a mock socket for testing."""
    sock = Mock(spec=socket.socket)
    sock.recv = Mock(return_value=b"")
    sock.settimeout = Mock()
    sock.connect = Mock()
    sock.sendall = Mock()
    sock.close = Mock()
    return sock


@pytest.fixture
def mock_unix_socket():
    """Create a mock Unix socket for Docker API testing."""
    sock = Mock(spec=socket.socket)
    sock.recv = Mock(return_value=b"")
    sock.settimeout = Mock()
    sock.connect = Mock()
    sock.sendall = Mock()
    sock.close = Mock()
    return sock


# ---------------------------------------------------------------------------
# Tests for _recv_all
# ---------------------------------------------------------------------------


class TestRecvAll:
    """Tests for _recv_all function."""

    def test_data_received(self, mock_socket):
        """Test normal data reception with multiple chunks."""
        mock_socket.recv.side_effect = [b"chunk1", b"chunk2", b"chunk3", b""]

        result = _recv_all(mock_socket)

        assert result == b"chunk1chunk2chunk3"
        assert mock_socket.recv.call_count == 4
        mock_socket.settimeout.assert_called_once()

    def test_socket_timeout(self, mock_socket):
        """Test socket timeout during reception."""
        mock_socket.recv.side_effect = socket.timeout()

        result = _recv_all(mock_socket)

        assert result == b""
        mock_socket.settimeout.assert_called_once()

    def test_empty_recv_eof(self, mock_socket):
        """Test immediate EOF (empty recv)."""
        mock_socket.recv.return_value = b""

        result = _recv_all(mock_socket)

        assert result == b""
        mock_socket.recv.assert_called_once()

    def test_single_chunk(self, mock_socket):
        """Test receiving a single chunk of data."""
        mock_socket.recv.side_effect = [b"single", b""]

        result = _recv_all(mock_socket)

        assert result == b"single"


# ---------------------------------------------------------------------------
# Tests for _parse_json_response
# ---------------------------------------------------------------------------


class TestParseJsonResponse:
    """Tests for _parse_json_response function."""

    def test_normal_json(self):
        """Test parsing normal JSON response with Content-Length."""
        raw = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 13\r\n\r\n{"key": "value"}'

        result = _parse_json_response(raw)

        assert result == {"key": "value"}

    def test_chunked_transfer(self):
        """Test parsing chunked transfer encoding response."""
        body = b'11\r\n{"key": "value"}\r\n0\r\n\r\n'
        raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body

        result = _parse_json_response(raw)

        assert result == {"key": "value"}

    def test_empty_body(self):
        """Test parsing response with empty body."""
        raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"

        result = _parse_json_response(raw)

        assert result is None

    def test_invalid_json(self):
        """Test parsing response with invalid JSON."""
        raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{invalid json}"

        result = _parse_json_response(raw)

        assert result is None

    def test_no_header_body_separator(self):
        """Test parsing response without header/body separator."""
        raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json"

        result = _parse_json_response(raw)

        assert result is None

    def test_non_utf8_body(self):
        """Test parsing response with non-UTF8 encoded body."""
        raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n\x80\x81\x82"

        result = _parse_json_response(raw)

        assert result is None

    def test_empty_raw(self):
        """Test parsing empty raw response."""
        raw = b""

        result = _parse_json_response(raw)

        assert result is None

    def test_chunked_multiple_chunks(self):
        """Test parsing multiple chunks that form valid JSON."""
        # {"a":"val"} = 12 bytes, split into 5 + 7
        body = b'5\r\n{"a":\r\n7\r\n"val"}\r\n0\r\n\r\n'
        raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body

        result = _parse_json_response(raw)

        assert result == {"a": "val"}


# ---------------------------------------------------------------------------
# Tests for _decode_chunked
# ---------------------------------------------------------------------------


class TestDecodeChunked:
    """Tests for _decode_chunked function."""

    def test_valid_single_chunk(self):
        """Test decoding valid single chunk with JSON."""
        # '"test"' = 6 bytes as JSON string
        body = b'6\r\n"test"\r\n0\r\n\r\n'

        result = _decode_chunked(body)

        assert result == "test"

    def test_multiple_chunks(self):
        """Test decoding multiple chunks that form valid JSON."""
        # {"a":"val"} = 12 bytes, split into 5 + 7
        body = b'5\r\n{"a":\r\n7\r\n"val"}\r\n0\r\n\r\n'

        result = _decode_chunked(body)

        assert result == {"a": "val"}

    def test_zero_terminal_chunk(self):
        """Test decoding with zero terminal chunk."""
        # {"ok":true} = 12 bytes (0xc hex)
        body = b'c\r\n{"ok":true}\r\n0\r\n\r\n'

        result = _decode_chunked(body)

        assert result == {"ok": True}

    def test_incomplete_chunk(self):
        """Test decoding incomplete chunk (truncated)."""
        # Chunk says 5 bytes but only 4 provided + no terminal
        body = b'5\r\n{"ok"'

        result = _decode_chunked(body)

        # Incomplete data, won't parse as JSON
        assert result is None

    def test_invalid_hex_size_line(self):
        """Test that content like 'abc123' is NOT treated as a hex size line.

        CRITICAL: This verifies the old bug is fixed - arbitrary content should NOT
        be interpreted as chunk size. Only content at the START of a chunk position
        should be treated as a size line.
        """
        # "abc123" is NOT valid hex for a size - 'g' is not hex
        # This should NOT be parsed as a chunk size
        body = b"abc123"

        result = _decode_chunked(body)

        # Should treat "abc123" as raw data, not as chunk header
        # "abc123" as raw data won't parse as JSON, so result should be None
        assert result is None

    def test_empty_body(self):
        """Test decoding empty body."""
        body = b""

        result = _decode_chunked(body)

        assert result is None

    def test_json_object_chunked(self):
        """Test decoding JSON object in chunked format."""
        body = b'7\r\n{"a":1}\r\n0\r\n\r\n'

        result = _decode_chunked(body)

        assert result == {"a": 1}

    def test_invalid_json_after_decode(self):
        """Test decoding valid chunks but invalid JSON content."""
        body = b"8\r\n{invalid}\r\n0\r\n\r\n"

        result = _decode_chunked(body)

        assert result is None


# ---------------------------------------------------------------------------
# Tests for _http_get
# ---------------------------------------------------------------------------


class TestHttpGet:
    """Tests for _http_get function."""

    @patch("stats.socket.socket")
    def test_successful_tcp_request_with_json(self, mock_socket_class):
        """Test successful TCP request returning JSON."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock

        # Simulate HTTP response
        response = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"status": "ok"}'
        mock_sock.recv.side_effect = [response, b""]

        result = _http_get("localhost", 8080, "/test")

        assert result == {"status": "ok"}
        mock_sock.connect.assert_called_once()
        mock_sock.sendall.assert_called_once()

    @patch("stats.socket.socket")
    def test_connection_refused(self, mock_socket_class):
        """Test connection refused error."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.side_effect = ConnectionRefusedError("Connection refused")

        result = _http_get("localhost", 8080, "/test")

        assert result is None

    @patch("stats.socket.socket")
    def test_socket_timeout(self, mock_socket_class):
        """Test socket timeout error."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.side_effect = socket.timeout("Timeout")

        result = _http_get("localhost", 8080, "/test")

        assert result is None

    @patch("stats.socket.socket")
    def test_with_extra_headers(self, mock_socket_class):
        """Test request with extra headers."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock

        response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}"
        mock_sock.recv.side_effect = [response, b""]

        result = _http_get("localhost", 8080, "/test", extra_headers={"Authorization": "Bearer token"})

        assert result == {}
        # Verify extra header was included in request
        call_args = mock_sock.sendall.call_args
        assert b"Authorization: Bearer token" in call_args[0][0]


# ---------------------------------------------------------------------------
# Tests for _docker_api_get
# ---------------------------------------------------------------------------


class TestDockerApiGet:
    """Tests for _docker_api_get function."""

    def _mock_socket_module(self, mock_socket_class):
        """Create a mock socket module with AF_UNIX for Windows."""
        import stats as stats_mod

        original = stats_mod.socket
        mock_mod = Mock()
        mock_mod.AF_UNIX = 1
        mock_mod.SOCK_STREAM = 2
        mock_mod.socket = mock_socket_class
        stats_mod.socket = mock_mod
        return stats_mod, original

    @patch("stats.socket.socket")
    def test_successful_unix_socket_request(self, mock_socket_class):
        """Test successful Unix socket request."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock

        response = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"Id": "abc123"}'
        mock_sock.recv.side_effect = [response, b""]

        import stats as stats_mod

        _, original = self._mock_socket_module(mock_socket_class)
        try:
            result = _docker_api_get("/containers/json")
            assert result == {"Id": "abc123"}
            mock_sock.connect.assert_called_once()
        finally:
            stats_mod.socket = original

    @patch("stats.socket.socket")
    def test_socket_error(self, mock_socket_class):
        """Test Unix socket error."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.side_effect = OSError("Socket error")

        import stats as stats_mod

        _, original = self._mock_socket_module(mock_socket_class)
        try:
            result = _docker_api_get("/containers/json")
            assert result is None
        finally:
            stats_mod.socket = original

    @patch("stats.socket.socket")
    def test_invalid_path(self, mock_socket_class):
        """Test request to invalid Docker API path."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock

        response = b'HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\n\r\n{"message": "404"}'
        mock_sock.recv.side_effect = [response, b""]

        import stats as stats_mod

        _, original = self._mock_socket_module(mock_socket_class)
        try:
            result = _docker_api_get("/invalid/path")
            assert result == {"message": "404"}
        finally:
            stats_mod.socket = original

    @patch("stats.socket.socket")
    def test_socket_error(self, mock_socket_class):
        """Test Unix socket error."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.side_effect = OSError("Socket error")

        import stats as stats_mod

        original_socket = stats_mod.socket
        try:
            mock_socket_module = Mock()
            mock_socket_module.AF_UNIX = 1
            mock_socket_module.SOCK_STREAM = 2
            mock_socket_module.socket = mock_socket_class
            stats_mod.socket = mock_socket_module

            result = _docker_api_get("/containers/json")

            assert result is None
        finally:
            stats_mod.socket = original_socket

    @patch("stats.socket.socket")
    def test_invalid_path(self, mock_socket_class):
        """Test request to invalid Docker API path."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock

        # Docker API returns 404 for invalid paths
        response = b'HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\n\r\n{"message": "404"}'
        mock_sock.recv.side_effect = [response, b""]

        import stats as stats_mod

        original_socket = stats_mod.socket
        try:
            mock_socket_module = Mock()
            mock_socket_module.AF_UNIX = 1
            mock_socket_module.SOCK_STREAM = 2
            mock_socket_module.socket = mock_socket_class
            stats_mod.socket = mock_socket_module

            result = _docker_api_get("/invalid/path")

            # Should return parsed JSON (even if it's an error response)
            assert result == {"message": "404"}
        finally:
            stats_mod.socket = original_socket


# ---------------------------------------------------------------------------
# Tests for fetch_container_stats
# ---------------------------------------------------------------------------


class TestFetchContainerStats:
    """Tests for fetch_container_stats function."""

    @patch("stats._docker_api_get")
    def test_running_containers_with_full_stats(self, mock_docker_get):
        """Test fetching stats for running containers with complete data."""
        # Mock container list
        containers = [
            {
                "Id": "abc123",
                "Names": ["/container1"],
                "State": "running",
                "Image": "nginx:latest",
            }
        ]

        # Mock container stats
        stats = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 1000000},
                "system_cpu_usage": 2000000,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 500000},
                "system_cpu_usage": 1000000,
            },
            "memory_stats": {"usage": 1048576, "limit": 2097152},
            "networks": {"eth0": {"rx_bytes": 1000, "tx_bytes": 500}},
        }

        mock_docker_get.side_effect = [containers, stats]

        result = fetch_container_stats()

        assert len(result) == 1
        assert result[0]["name"] == "container1"
        assert result[0]["status"] == "running"
        assert result[0]["image"] == "nginx:latest"
        assert result[0]["cpu_percent"] > 0
        assert result[0]["mem_usage"] == 1048576
        assert result[0]["mem_limit"] == 2097152
        assert result[0]["net_rx"] == 1000
        assert result[0]["net_tx"] == 500

    @patch("stats._docker_api_get")
    def test_no_containers_returned(self, mock_docker_get):
        """Test when Docker API returns no containers."""
        mock_docker_get.return_value = []

        result = fetch_container_stats()

        assert result == []

    @patch("stats._docker_api_get")
    def test_containers_list_but_stats_fetch_fails(self, mock_docker_get):
        """Test when containers list succeeds but stats fetch fails."""
        containers = [
            {
                "Id": "abc123",
                "Names": ["/container1"],
                "State": "running",
                "Image": "nginx:latest",
            }
        ]

        # First call returns containers, second call (for stats) returns None
        mock_docker_get.side_effect = [containers, None]

        result = fetch_container_stats()

        assert len(result) == 1
        assert result[0]["name"] == "container1"
        assert result[0]["cpu_percent"] == 0.0
        assert result[0]["mem_usage"] == 0

    @patch("stats._docker_api_get")
    def test_non_running_containers(self, mock_docker_get):
        """Test filtering of non-running containers."""
        containers = [
            {
                "Id": "abc123",
                "Names": ["/stopped_container"],
                "State": "exited",
                "Image": "nginx:latest",
            }
        ]

        mock_docker_get.return_value = containers

        result = fetch_container_stats()

        assert len(result) == 1
        assert result[0]["name"] == "stopped_container"
        assert result[0]["status"] == "exited"
        assert result[0]["cpu_percent"] == 0.0

    @patch("stats._docker_api_get")
    def test_partial_stats_data(self, mock_docker_get):
        """Test handling of partial stats data (missing keys like online_cpus)."""
        containers = [
            {
                "Id": "abc123",
                "Names": ["/container1"],
                "State": "running",
                "Image": "nginx:latest",
            }
        ]

        # Stats missing online_cpus
        stats = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 1000000},
                "system_cpu_usage": 2000000,
                # online_cpus missing - should default to 1
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 500000},
                "system_cpu_usage": 1000000,
            },
            "memory_stats": {"usage": 1048576, "limit": 2097152},
        }

        mock_docker_get.side_effect = [containers, stats]

        result = fetch_container_stats()

        assert len(result) == 1
        assert result[0]["cpu_percent"] > 0  # Should use default online_cpus=1
        assert result[0]["mem_usage"] == 1048576

    @patch("stats._docker_api_get")
    def test_docker_api_returns_none(self, mock_docker_get):
        """Test when Docker API returns None."""
        mock_docker_get.return_value = None

        result = fetch_container_stats()

        assert result == []


# ---------------------------------------------------------------------------
# Tests for fetch_litellm_analytics
# ---------------------------------------------------------------------------


class TestFetchLitellmAnalytics:
    """Tests for fetch_litellm_analytics function."""

    @patch("stats._query_request_counts")
    @patch("stats._http_get")
    def test_all_endpoints_return_data(self, mock_http_get, mock_db):
        """Test when all LiteLLM endpoints return data."""
        mock_db.return_value = [
            {"model": "gpt-4", "requests": 100, "total_spend": 50},
        ]
        mock_http_get.side_effect = [
            [{"model": "gpt-4", "total_spend": 50}],  # spend_models (unused, DB wins)
            {"total_spend": 100},  # global_spend
            {"status": "healthy", "ping_response": True},  # cache
            [{"model": "gpt-4", "spend": 50}],  # spend_logs
        ]

        result = fetch_litellm_analytics()

        assert result["spend_by_model"] == [
            {"model": "gpt-4", "requests": 100, "total_spend": 50},
        ]
        assert result["global_spend"] == {"total_spend": 100}
        assert result["cache_health"]["status"] == "healthy"
        assert result["spend_logs"] == [{"model": "gpt-4", "spend": 50}]

    @patch("stats._query_request_counts")
    @patch("stats._http_get")
    def test_some_endpoints_fail_partial(self, mock_http_get, mock_db):
        """Test when some endpoints fail (partial data)."""
        mock_db.return_value = [
            {"model": "gpt-4", "requests": 50, "total_spend": 0},
        ]
        mock_http_get.side_effect = [
            None,  # spend_models - fail (DB still provides data)
            {"total_spend": 100},  # global_spend - success
            None,  # cache - fail
            None,  # spend_logs - fail
        ]

        result = fetch_litellm_analytics()

        assert result["spend_by_model"] == [
            {"model": "gpt-4", "requests": 50, "total_spend": 0},
        ]
        assert result["global_spend"] == {"total_spend": 100}
        assert result["cache_health"]["status"] == "unknown"
        assert result["spend_logs"] == []

    @patch("stats._query_request_counts")
    @patch("stats._http_get")
    def test_all_endpoints_fail(self, mock_http_get, mock_db):
        """Test when all endpoints fail."""
        mock_db.return_value = []
        mock_http_get.side_effect = [None, None, None, None]

        result = fetch_litellm_analytics()

        assert result["spend_by_model"] == []
        assert result["global_spend"] == {}
        assert result["cache_health"]["status"] == "unknown"
        assert result["spend_logs"] == []

    @patch("stats._query_request_counts")
    @patch("stats._http_get")
    def test_db_takes_priority_over_api(self, mock_http_get, mock_db):
        """Test that DB request counts override API spend_models data."""
        mock_db.return_value = [
            {"model": "glm-5-turbo", "requests": 500, "total_spend": 0},
            {"model": "qwen3.5", "requests": 200, "total_spend": 0},
        ]
        mock_http_get.side_effect = [
            [{"model": "old-data", "total_spend": 999}],  # API data should be ignored
            {"total_spend": 0},
            {"status": "healthy"},
            [],
        ]

        result = fetch_litellm_analytics()

        assert len(result["spend_by_model"]) == 2
        assert result["spend_by_model"][0]["requests"] == 500


# ---------------------------------------------------------------------------
# Tests for _empty_analytics
# ---------------------------------------------------------------------------


class TestEmptyAnalytics:
    """Tests for _empty_analytics function."""

    def test_returns_expected_default_structure(self):
        """Test that _empty_analytics returns the expected default structure."""
        result = _empty_analytics()

        assert "spend_by_model" in result
        assert result["spend_by_model"] == []
        assert "global_spend" in result
        assert result["global_spend"] == {}
        assert "cache_health" in result
        assert result["cache_health"]["status"] == "error"
        assert result["cache_health"]["cache_type"] == "none"
        assert result["cache_health"]["ping"] is False
        assert result["cache_health"]["set_cache"] == "error"
        assert result["spend_logs"] == []


# ---------------------------------------------------------------------------
# Tests for fetch_combined
# ---------------------------------------------------------------------------


class TestQueryRequestCounts:
    """Tests for _query_request_counts function (direct DB query)."""

    @patch("stats.PSYCOPG_AVAILABLE", False)
    def test_psycopg2_not_available(self):
        """Test graceful fallback when psycopg2 is not installed."""
        result = _query_request_counts()
        assert result == []

    def test_successful_query(self):
        """Test successful DB query returns request counts."""
        with patch("stats.PSYCOPG_AVAILABLE", True), patch("stats.psycopg2", create=True) as mock_pg:
            mock_conn = Mock()
            mock_cursor = Mock()
            mock_pg.connect.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            # Simulate DB returning provider-prefixed names
            mock_cursor.fetchall.return_value = [
                ("openai/glm-5-turbo", 500, 0.0),
                ("glm-5-turbo", 44, 0.0),
                ("openai/qwen3.5-122b", 200, 0.0),
            ]

            result = _query_request_counts()

            # openai/ prefix stripped, duplicates merged
            assert len(result) == 2
            assert result[0] == {"model": "glm-5-turbo", "requests": 544, "total_spend": 0.0}
            assert result[1] == {"model": "qwen3.5-122b", "requests": 200, "total_spend": 0.0}
            mock_cursor.close.assert_called_once()
            mock_conn.close.assert_called_once()

    @patch("stats.psycopg2", create=True)
    def test_db_connection_error(self, mock_psycopg2):
        """Test graceful handling of DB connection failure."""
        mock_psycopg2.connect.side_effect = Exception("Connection refused")

        result = _query_request_counts()

        assert result == []

    @patch("stats.psycopg2", create=True)
    def test_empty_result(self, mock_psycopg2):
        """Test handling of empty query result."""
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_psycopg2.connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        result = _query_request_counts()

        assert result == []


class TestFetchCombined:
    """Tests for fetch_combined function."""

    @patch("stats.fetch_container_stats")
    @patch("stats.fetch_litellm_analytics")
    def test_both_succeed(self, mock_analytics, mock_containers):
        """Test when both containers and analytics succeed."""
        mock_containers.return_value = [{"name": "test"}]
        mock_analytics.return_value = {"global_spend": {}}

        result = fetch_combined()

        assert result["containers"] == [{"name": "test"}]
        assert "analytics" in result

    @patch("stats.fetch_container_stats")
    @patch("stats.fetch_litellm_analytics")
    def test_containers_fail(self, mock_analytics, mock_containers):
        """Test when containers fetch fails."""
        mock_containers.return_value = []
        mock_analytics.return_value = {"global_spend": {}}

        result = fetch_combined()

        assert result["containers"] == []
        assert "analytics" in result

    @patch("stats.fetch_container_stats")
    @patch("stats.fetch_litellm_analytics")
    def test_analytics_fail(self, mock_analytics, mock_containers):
        """Test when analytics fetch fails."""
        mock_containers.return_value = [{"name": "test"}]
        mock_analytics.return_value = None

        result = fetch_combined()

        assert result["containers"] == [{"name": "test"}]
        # Should get empty analytics when fetch fails
        assert result["analytics"]["cache_health"]["status"] == "error"

    @patch("stats.fetch_container_stats")
    @patch("stats.fetch_litellm_analytics")
    def test_both_fail(self, mock_analytics, mock_containers):
        """Test when both containers and analytics fail."""
        mock_containers.return_value = []
        mock_analytics.return_value = None

        result = fetch_combined()

        assert result["containers"] == []
        assert result["analytics"]["cache_health"]["status"] == "error"


# ---------------------------------------------------------------------------
# Tests for _Handler (HTTP server)
# ---------------------------------------------------------------------------


class TestHandler:
    """Tests for _Handler HTTP request handler."""

    def _make_handler(self, path="/api/docker-stats"):
        """Create a handler with mocked I/O for testing."""
        mock_conn = Mock()
        mock_conn.makefile.return_value = io.BytesIO(b"GET " + path.encode() + b" HTTP/1.1\r\n\r\n")
        wfile = io.BytesIO()
        server = Mock()

        handler = _Handler(mock_conn, ("127.0.0.1", 12345), server)
        handler.wfile = wfile
        return handler, wfile

    def _get_json_body(self, wfile):
        """Extract JSON body from wfile, skipping HTTP headers."""
        raw = wfile.getvalue()
        # HTTP response headers end with \r\n\r\n
        idx = raw.find(b"\r\n\r\n")
        if idx != -1:
            raw = raw[idx + 4 :]
        return json.loads(raw)

    @patch("stats.fetch_container_stats")
    def test_route_docker_stats(self, mock_fetch):
        """Test /api/docker-stats route dispatch."""
        mock_fetch.return_value = [{"name": "test"}]

        handler, wfile = self._make_handler("/api/docker-stats")
        handler.path = "/api/docker-stats"
        handler.do_GET()

        response = self._get_json_body(wfile)
        assert response == [{"name": "test"}]

    @patch("stats.fetch_litellm_analytics")
    def test_route_litellm_analytics(self, mock_fetch):
        """Test /api/litellm-analytics route dispatch."""
        mock_fetch.return_value = {"global_spend": {}}

        handler, wfile = self._make_handler("/api/litellm-analytics")
        handler.path = "/api/litellm-analytics"
        handler.do_GET()

        response = self._get_json_body(wfile)
        assert response == {"global_spend": {}}

    @patch("stats.fetch_combined")
    def test_route_api_all(self, mock_fetch):
        """Test /api/all route dispatch."""
        mock_fetch.return_value = {"containers": [], "analytics": {}}

        handler, wfile = self._make_handler("/api/all")
        handler.path = "/api/all"
        handler.do_GET()

        response = self._get_json_body(wfile)
        assert response == {"containers": [], "analytics": {}}

    def test_404_unknown_route(self):
        """Test 404 response for unknown routes."""
        handler, wfile = self._make_handler("/api/unknown")
        handler.path = "/api/unknown"
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler.do_GET()

        handler.send_response.assert_called_with(404)
        handler.end_headers.assert_called()

    @patch("stats.fetch_container_stats")
    def test_500_on_serialization_error(self, mock_fetch):
        """Test 500 response on JSON serialization error."""
        mock_fetch.return_value = [{"data": lambda x: x}]

        handler, wfile = self._make_handler("/api/docker-stats")
        handler.path = "/api/docker-stats"
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler.do_GET()

        handler.send_response.assert_called_with(500)
        handler.end_headers.assert_called()

    @patch("stats.fetch_container_stats")
    def test_500_on_serialization_error(self, mock_fetch):
        """Test 500 response on JSON serialization error."""
        # Create data that can't be serialized
        mock_fetch.return_value = [{"data": lambda x: x}]

        handler, wfile = self._make_handler("/api/docker-stats")
        handler.path = "/api/docker-stats"
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler.do_GET()

        handler.send_response.assert_called_with(500)
        handler.end_headers.assert_called()


# ---------------------------------------------------------------------------
# Integration-style tests with mocked HTTP server
# ---------------------------------------------------------------------------


class TestHandlerIntegration:
    """Integration-style tests for _Handler with actual HTTP response."""

    def _make_handler(self, path="/api/docker-stats"):
        """Create a handler with mocked I/O for testing."""
        mock_conn = Mock()
        mock_conn.makefile.return_value = io.BytesIO(b"GET " + path.encode() + b" HTTP/1.1\r\n\r\n")
        wfile = io.BytesIO()
        server = Mock()

        handler = _Handler(mock_conn, ("127.0.0.1", 12345), server)
        handler.wfile = wfile
        return handler, wfile

    def _get_json_body(self, wfile):
        """Extract JSON body from wfile, skipping HTTP headers."""
        raw = wfile.getvalue()
        idx = raw.find(b"\r\n\r\n")
        if idx != -1:
            raw = raw[idx + 4 :]
        return json.loads(raw)

    def test_docker_stats_route_returns_json(self):
        """Test that docker-stats route returns proper JSON."""
        with patch("stats.fetch_container_stats") as mock_fetch:
            mock_fetch.return_value = [{"name": "test", "cpu_percent": 50.0}]

            handler, wfile = self._make_handler("/api/docker-stats")
            handler.path = "/api/docker-stats"
            handler.do_GET()

            response = self._get_json_body(wfile)
            assert len(response) == 1
            assert response[0]["name"] == "test"

    def test_all_route_returns_combined_data(self):
        """Test that /api/all returns combined container and analytics data."""
        with (
            patch("stats.fetch_container_stats") as mock_containers,
            patch("stats.fetch_litellm_analytics") as mock_analytics,
        ):
            mock_containers.return_value = [{"name": "container1"}]
            mock_analytics.return_value = {"global_spend": {"total": 100}}

            handler, wfile = self._make_handler("/api/all")
            handler.path = "/api/all"
            handler.do_GET()

            response = self._get_json_body(wfile)
            assert "containers" in response
            assert "analytics" in response


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case tests for stats functions."""

    def test_decode_chunked_with_extensions(self):
        """Test chunked encoding with chunk extensions."""
        # Chunk with extension after size (should be ignored)
        body = b"5;name=value\r\nhello\r\n0\r\n\r\n"

        result = _decode_chunked(body)

        # The semicolon makes it invalid hex, so treated as raw data
        assert result is None

    def test_parse_json_with_multiple_headers(self):
        """Test parsing JSON with multiple HTTP headers."""
        raw = b"""HTTP/1.1 200 OK\r
Content-Type: application/json\r
Content-Length: 17\r
X-Custom-Header: value\r
\r
{"message": "ok"}"""

        result = _parse_json_response(raw)

        assert result == {"message": "ok"}

    def test_recv_all_with_large_chunks(self, mock_socket):
        """Test receiving large chunks."""
        large_data = b"x" * 10000
        mock_socket.recv.side_effect = [large_data, b""]

        result = _recv_all(mock_socket)

        assert result == large_data
        assert len(result) == 10000

    @patch("stats.socket.socket")
    def test_http_get_with_chunked_response(self, mock_socket_class):
        """Test _http_get with chunked transfer encoding."""
        mock_sock = Mock()
        mock_socket_class.return_value = mock_sock

        # '{"ok":1}' = 8 bytes
        response = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n8\r\n{"ok":1}\r\n0\r\n\r\n'
        mock_sock.recv.side_effect = [response, b""]

        result = _http_get("localhost", 8080, "/test")

        assert result == {"ok": 1}

    def test_decode_chunked_preserves_binary_data(self):
        """Test that chunked decoding preserves binary data."""
        # Binary data in chunks
        body = b"4\r\n\x00\x01\x02\x03\r\n0\r\n\r\n"

        result = _decode_chunked(body)

        # Binary won't decode as UTF-8, so should return None
        assert result is None

    @patch("stats._docker_api_get")
    def test_container_with_multiple_networks(self, mock_docker_get):
        """Test container stats aggregation across multiple networks."""
        containers = [
            {
                "Id": "abc123",
                "Names": ["/container1"],
                "State": "running",
                "Image": "nginx:latest",
            }
        ]

        stats = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 0},
                "system_cpu_usage": 0,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 0},
                "system_cpu_usage": 0,
            },
            "memory_stats": {"usage": 0, "limit": 0},
            "networks": {
                "eth0": {"rx_bytes": 1000, "tx_bytes": 500},
                "eth1": {"rx_bytes": 2000, "tx_bytes": 1000},
            },
        }

        mock_docker_get.side_effect = [containers, stats]

        result = fetch_container_stats()

        assert result[0]["net_rx"] == 3000  # Sum of both interfaces
        assert result[0]["net_tx"] == 1500

    @patch("stats._docker_api_get")
    def test_container_with_zero_memory_limit(self, mock_docker_get):
        """Test container stats with zero memory limit (avoid division by zero)."""
        containers = [
            {
                "Id": "abc123",
                "Names": ["/container1"],
                "State": "running",
                "Image": "nginx:latest",
            }
        ]

        stats = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 0},
                "system_cpu_usage": 0,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 0},
                "system_cpu_usage": 0,
            },
            "memory_stats": {"usage": 100, "limit": 0},  # Zero limit
        }

        mock_docker_get.side_effect = [containers, stats]

        result = fetch_container_stats()

        # Should handle zero division gracefully
        assert result[0]["mem_percent"] == 0.0
