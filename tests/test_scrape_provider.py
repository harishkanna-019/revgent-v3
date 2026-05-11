"""Scrape provider tests — real fetches when available, pure SSRF tests always."""

import asyncio
import ipaddress
import socket

import pytest
import pytest_asyncio

from providers.scrape import (
    MAX_REDIRECTS,
    MIN_CONTENT_LENGTH,
    SSRFBlocked,
    ScrapeError,
    _follow_redirects_safely,
    _is_public_ip,
    _passes_quality_gate,
    _resolve_host,
    _validate_url_for_ssrf,
)
from providers import scrape

pytestmark = pytest.mark.asyncio

# Skip real-network tests when offline
_HAS_NETWORK = False
try:
    socket.create_connection(("example.com", 443), timeout=2)
    _HAS_NETWORK = True
except Exception:
    pass

skip_if_no_network = pytest.mark.skipif(not _HAS_NETWORK, reason="No network access")


# ── Helpers ──

@pytest_asyncio.fixture(autouse=True)
async def _init_scrape():
    """Ensure scrape provider is initialized before each test."""
    await scrape.init()
    yield
    await scrape.close()


# ── SSRF Protection Tests (pure, no network) ──

class TestSSRFScheme:
    """SSRF scheme validation."""

    def test_http_allowed(self):
        _validate_url_for_ssrf("http://example.com")

    def test_https_allowed(self):
        _validate_url_for_ssrf("https://example.com")

    def test_file_scheme_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("file:///etc/passwd")
        assert "scheme" in str(exc.value).lower()

    def test_ftp_scheme_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("ftp://example.com")
        assert "scheme" in str(exc.value).lower()

    def test_gopher_scheme_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("gopher://example.com")
        assert "scheme" in str(exc.value).lower()


class TestSSRFLocalhost:
    """SSRF localhost blocking."""

    def test_localhost_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://localhost:8080/secret")
        assert "localhost" in str(exc.value).lower()

    def test_localhost_subdomain_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://api.localhost/path")
        assert "localhost" in str(exc.value).lower()

    def test_127_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://127.0.0.1/admin")
        assert "non-public" in str(exc.value).lower() or "localhost" in str(exc.value).lower()


class TestSSRFPrivateIPs:
    """SSRF private IP blocking."""

    def test_10_x_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://10.0.0.1/")
        assert "non-public" in str(exc.value).lower()

    def test_172_16_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://172.16.0.1/")
        assert "non-public" in str(exc.value).lower()

    def test_172_31_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://172.31.255.255/")
        assert "non-public" in str(exc.value).lower()

    def test_192_168_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://192.168.1.1/")
        assert "non-public" in str(exc.value).lower()

    def test_link_local_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://169.254.1.1/")
        assert "non-public" in str(exc.value).lower()

    def test_loopback_127_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://127.0.0.1/")
        assert "non-public" in str(exc.value).lower()

    def test_loopback_127_1_blocked(self):
        with pytest.raises(SSRFBlocked) as exc:
            _validate_url_for_ssrf("http://127.1.0.0/")
        assert "non-public" in str(exc.value).lower()


class TestSSRFPublicIPs:
    """SSRF allows public IPs."""

    def test_example_com_allowed(self):
        # example.com is public — should not raise
        _validate_url_for_ssrf("http://example.com")

    def test_google_allowed(self):
        _validate_url_for_ssrf("https://google.com")


class TestIsPublicIP:
    """Tests for _is_public_ip helper."""

    def test_public_ipv4(self):
        assert _is_public_ip("8.8.8.8") is True
        assert _is_public_ip("1.1.1.1") is True

    def test_private_ipv4(self):
        assert _is_public_ip("10.0.0.1") is False
        assert _is_public_ip("192.168.1.1") is False
        assert _is_public_ip("172.16.0.1") is False

    def test_loopback_ipv4(self):
        assert _is_public_ip("127.0.0.1") is False
        assert _is_public_ip("127.255.255.255") is False

    def test_link_local_ipv4(self):
        assert _is_public_ip("169.254.1.1") is False

    def test_multicast_ipv4(self):
        assert _is_public_ip("224.0.0.1") is False

    def test_unspecified_ipv4(self):
        assert _is_public_ip("0.0.0.0") is False

    def test_public_ipv6(self):
        assert _is_public_ip("2001:4860:4860::8888") is True

    def test_loopback_ipv6(self):
        assert _is_public_ip("::1") is False

    def test_invalid_ip(self):
        assert _is_public_ip("not-an-ip") is False


# ── Quality Gate Tests ──

class TestQualityGate:
    """Tests for article quality gate."""

    def test_short_text_rejected(self):
        """Text shorter than 80 chars is rejected."""
        assert _passes_quality_gate("x" * 79) is False
        assert _passes_quality_gate("") is False
        assert _passes_quality_gate(None) is False  # type: ignore

    def test_min_length_passes(self):
        """Text of exactly 80 chars passes."""
        assert _passes_quality_gate("x" * 80) is True

    def test_404_marker_rejected(self):
        text = "x" * 100 + " 404 error " + "x" * 100
        assert _passes_quality_gate(text) is False

    def test_page_not_found_rejected(self):
        text = "x" * 100 + " page not found " + "x" * 100
        assert _passes_quality_gate(text) is False

    def test_access_denied_rejected(self):
        text = "x" * 100 + " access denied " + "x" * 100
        assert _passes_quality_gate(text) is False

    def test_enable_javascript_rejected(self):
        text = "x" * 100 + " enable javascript " + "x" * 100
        assert _passes_quality_gate(text) is False

    def test_please_enable_cookies_rejected(self):
        text = "x" * 100 + " please enable cookies " + "x" * 100
        assert _passes_quality_gate(text) is False

    def test_clean_text_passes(self):
        text = "This is a normal article about technology and business. " * 10
        assert _passes_quality_gate(text) is True

    def test_case_insensitive_markers(self):
        text = "x" * 100 + " PAGE NOT FOUND " + "x" * 100
        assert _passes_quality_gate(text) is False


# ── DNS Cache Tests ──

class TestDNSCache:
    """Tests for DNS resolution caching."""

    def test_resolve_host_returns_ips(self):
        ips = _resolve_host("example.com")
        assert isinstance(ips, list)
        if ips:
            # Should be valid IP addresses
            for ip in ips:
                ipaddress.ip_address(ip)

    def test_resolve_host_caching(self):
        """lru_cache should return same result for same host."""
        ips1 = _resolve_host("example.com")
        ips2 = _resolve_host("example.com")
        assert ips1 == ips2

    def test_resolve_bad_host_returns_empty(self):
        ips = _resolve_host("this-host-definitely-does-not-exist-12345.local")
        assert ips == []


# ── Real Network Tests ──

@skip_if_no_network
class TestScrapeReal:
    """Tests that make actual HTTP requests."""

    async def test_scrape_example_com(self):
        """Scrape example.com returns some text."""
        text = await scrape.scrape("https://example.com")
        # example.com has minimal content — might fail quality gate
        # We just verify it doesn't raise
        assert isinstance(text, str)

    async def test_scrape_with_max_chars(self):
        """max_chars limits returned text."""
        text = await scrape.scrape("https://example.com", max_chars=50)
        assert len(text) <= 50

    async def test_scrape_many(self):
        """scrape_many returns results for multiple URLs."""
        urls = ["https://example.com", "https://example.org"]
        results = await scrape.scrape_many(urls)
        assert set(results.keys()) == set(urls)
        for url, text in results.items():
            assert isinstance(text, str)

    async def test_scrape_many_isolates_errors(self):
        """A bad URL doesn't abort other scrapes."""
        urls = [
            "https://example.com",
            "http://localhost:9999",  # SSRF blocked
        ]
        results = await scrape.scrape_many(urls)
        assert "https://example.com" in results
        assert "http://localhost:9999" in results
        assert results["http://localhost:9999"] == ""

    async def test_ssrf_blocks_before_request(self):
        """SSRF blocks are raised before any HTTP request."""
        with pytest.raises(SSRFBlocked):
            await scrape.scrape("http://127.0.0.1/secret")


# ── Lifecycle Tests ──

class TestScrapeLifecycle:
    """Tests for init() / close() lifecycle."""

    async def test_init_creates_client_and_semaphore(self):
        assert scrape._client is not None
        assert scrape._semaphore is not None

    async def test_close_releases_resources(self):
        await scrape.close()
        assert scrape._client is None
        assert scrape._semaphore is None

    async def test_init_idempotent(self):
        await scrape.init()
        client_first = scrape._client
        await scrape.init()
        assert scrape._client is client_first

    async def test_scrape_auto_init(self):
        await scrape.close()
        assert scrape._client is None
        if not _HAS_NETWORK:
            pytest.skip("No network")
        # scrape() should auto-init
        text = await scrape.scrape("https://example.com")
        assert isinstance(text, str)
        assert scrape._client is not None
