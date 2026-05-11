"""Scrape provider with SSRF protection and trafilatura extraction."""

import asyncio
import ipaddress
import os
import socket
from functools import lru_cache
from urllib.parse import urlparse

import httpx
import trafilatura

# ── Configuration ──

SCRAPE_CONCURRENCY = int(os.environ.get("SCRAPE_CONCURRENCY", "8"))
SCRAPE_TIMEOUT = 10.0  # seconds per page
MAX_REDIRECTS = 5
MIN_CONTENT_LENGTH = 80

# Error page markers that trigger quality gate rejection
ERROR_MARKERS = frozenset(
    {
        "404 error",
        "page not found",
        "access denied",
        "enable javascript",
        "please enable cookies",
    }
)

# ── Exceptions ──


class SSRFBlocked(Exception):
    """Raised when a URL targets a non-public IP address."""

    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(f"SSRF blocked: {url} — {reason}")


class ScrapeError(Exception):
    """Raised on network failure (timeout, HTTP 5xx, connection refused)."""

    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(f"Scrape failed: {url} — {reason}")


# ── Module state ──

_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None


# ── SSRF Protection ──


@lru_cache(maxsize=2048)
def _resolve_host(hostname: str) -> list[str]:
    """Resolve hostname to list of IP addresses.

    Cached with lru_cache to avoid repeated DNS lookups.
    """
    try:
        # getaddrinfo returns tuples of (family, type, proto, canonname, sockaddr)
        # sockaddr is (ip, port) for IPv4 or (ip, port, flow, scope) for IPv6
        infos = socket.getaddrinfo(hostname, None)
        ips: list[str] = []
        for info in infos:
            sockaddr = info[4]
            # sockaddr[0] is always a string IP for getaddrinfo
            ip: str = sockaddr[0]  # type: ignore[assignment]
            if ip not in ips:
                ips.append(ip)
        return ips
    except socket.gaierror:
        return []


def _is_public_ip(ip_str: str) -> bool:
    """Check if an IP address is public-routable."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        return False


def _validate_url_for_ssrf(url: str) -> None:
    """Validate a URL against SSRF attacks.

    Checks:
    1. Scheme is http or https
    2. Hostname is not localhost or *.localhost
    3. Hostname resolves to at least one IP
    4. Every resolved IP is public-routable

    Raises SSRFBlocked if any check fails.
    """
    parsed = urlparse(url)

    # 1. Scheme whitelist
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlocked(
            url, f"Scheme '{parsed.scheme}' not allowed (only http/https)"
        )

    hostname = parsed.hostname
    if not hostname:
        raise SSRFBlocked(url, "No hostname in URL")

    # 2. Block localhost by name
    if hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        raise SSRFBlocked(url, f"Host '{hostname}' is localhost")

    # 3. DNS resolution
    ips = _resolve_host(hostname)
    if not ips:
        raise SSRFBlocked(url, f"Host '{hostname}' could not be resolved")

    # 4. Check every IP is public
    for ip in ips:
        if not _is_public_ip(ip):
            raise SSRFBlocked(url, f"Host '{hostname}' resolves to non-public IP {ip}")


async def _follow_redirects_safely(
    client: httpx.AsyncClient,
    url: str,
    max_hops: int = MAX_REDIRECTS,
) -> httpx.Response:
    """Follow redirects manually, validating each hop for SSRF.

    Raises:
        SSRFBlocked: if a redirect target is blocked.
        ScrapeError: on network failure or too many redirects.
    """
    current_url = url
    for hop in range(max_hops + 1):
        _validate_url_for_ssrf(current_url)
        try:
            response = await client.get(
                current_url,
                follow_redirects=False,  # We handle redirects manually
            )
        except httpx.HTTPStatusError as exc:
            raise ScrapeError(current_url, f"HTTP {exc.response.status_code}") from exc
        except httpx.TimeoutException as exc:
            raise ScrapeError(
                current_url, f"Timed out after {SCRAPE_TIMEOUT}s"
            ) from exc
        except Exception as exc:
            raise ScrapeError(current_url, str(exc)) from exc

        # Check for redirect
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise ScrapeError(current_url, "Redirect without Location header")
            # Resolve relative URLs
            from urllib.parse import urljoin

            current_url = urljoin(current_url, location)
            continue

        # Not a redirect — return the response
        return response

    raise ScrapeError(url, f"Too many redirects (>{max_hops})")


# ── Quality Gate ──


def _passes_quality_gate(text: str) -> bool:
    """Check if extracted text passes the article quality gate.

    Rejects:
    - Text shorter than MIN_CONTENT_LENGTH chars
    - Text containing error page markers
    """
    if not text or len(text) < MIN_CONTENT_LENGTH:
        return False

    text_lower = text.lower()
    for marker in ERROR_MARKERS:
        if marker in text_lower:
            return False

    return True


# ── Lifecycle ──


async def init() -> None:
    """Initialize the scrape client and semaphore."""
    global _client, _semaphore
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(SCRAPE_TIMEOUT),
            follow_redirects=False,  # We handle redirects manually for SSRF
        )
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(SCRAPE_CONCURRENCY)


async def close() -> None:
    """Close the scrape client and release resources."""
    global _client, _semaphore
    if _client is not None:
        await _client.aclose()
        _client = None
    _semaphore = None


# ── Public interface ──


async def scrape(url: str, max_chars: int | None = None) -> str:
    """Scrape a single URL and extract article text.

    Args:
        url: The URL to scrape.
        max_chars: Optional maximum characters to return.

    Returns:
        Extracted article text, or empty string if the quality gate rejects it.

    Raises:
        SSRFBlocked: if the URL targets a non-public address.
        ScrapeError: on network timeout or HTTP 5xx.
    """
    # Lazy init
    if _client is None or _semaphore is None:
        await init()

    # SSRF validation (before any network request)
    _validate_url_for_ssrf(url)

    assert _client is not None
    assert _semaphore is not None

    # Fetch with semaphore
    async with _semaphore:
        response = await _follow_redirects_safely(_client, url)

    # Check for HTTP errors
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ScrapeError(url, f"HTTP {exc.response.status_code}") from exc

    # Extract text via trafilatura (CPU-bound — run in executor)
    html = response.text
    loop = asyncio.get_running_loop()
    text = await loop.run_in_executor(
        None,
        lambda: trafilatura.extract(html, include_comments=False, include_tables=False),
    )

    if text is None:
        text = ""

    # Quality gate
    if not _passes_quality_gate(text):
        return ""

    if max_chars is not None:
        text = text[:max_chars]

    return text


async def scrape_many(
    urls: list[str],
    max_chars: int | None = None,
) -> dict[str, str]:
    """Scrape multiple URLs concurrently with per-URL error isolation.

    Args:
        urls: List of URLs to scrape.
        max_chars: Optional maximum characters per result.

    Returns:
        Dict mapping each URL to its extracted text. Failed URLs map to
        empty string — errors are isolated per-URL.
    """
    if _client is None or _semaphore is None:
        await init()

    async def _scrape_one(url: str) -> tuple[str, str]:
        try:
            text = await scrape(url, max_chars=max_chars)
            return url, text
        except (SSRFBlocked, ScrapeError):
            return url, ""

    tasks = [asyncio.create_task(_scrape_one(url)) for url in urls]
    results = await asyncio.gather(*tasks)

    return {url: text for url, text in results}
