"""Company name tool tests — hit real OpenRouter. No mocks."""

import os

import pytest
import pytest_asyncio

from providers import llm
from tools import company

pytestmark = pytest.mark.asyncio

# Skip real-API tests when no key is available
_HAS_KEY = bool(os.environ.get("OPENROUTER_API_KEY"))
skip_if_no_key = pytest.mark.skipif(not _HAS_KEY, reason="OPENROUTER_API_KEY not set")


@pytest_asyncio.fixture(autouse=True)
async def _init_llm():
    """Ensure LLM provider is initialized before each test."""
    if _HAS_KEY:
        await llm.init()
    yield
    if _HAS_KEY:
        await llm.close()


@pytest_asyncio.fixture(autouse=True)
async def _clear_company_cache():
    """Clear company cache before each test."""
    company._company_cache.clear()


@skip_if_no_key
class TestGetNamesReal:
    """Tests that make actual LLM calls to extract company names."""

    async def test_meta_com(self):
        """meta.com returns variations including 'meta'."""
        names, usage = await company.get_names("meta.com")
        assert isinstance(names, list)
        assert len(names) >= 1
        assert names[0] == "meta"  # stem is first
        assert "meta" in names
        assert usage["total_tokens"] >= 0

    async def test_google_com(self):
        """google.com returns variations including 'google'."""
        names, usage = await company.get_names("google.com")
        assert isinstance(names, list)
        assert len(names) >= 1
        assert names[0] == "google"
        assert "google" in names

    async def test_microsoft_com(self):
        """microsoft.com returns variations including 'microsoft'."""
        names, usage = await company.get_names("microsoft.com")
        assert isinstance(names, list)
        assert len(names) >= 1
        assert names[0] == "microsoft"
        assert "microsoft" in names

    async def test_www_prefix_stripped(self):
        """www. prefix is stripped from domain."""
        names, usage = await company.get_names("www.apple.com")
        assert names[0] == "apple"
        assert "apple" in names

    async def test_caching_second_call_hits_cache(self):
        """Second call with same domain hits cache (no LLM call)."""
        names1, usage1 = await company.get_names("example-cache-test.com")
        # The stem for this domain
        assert names1[0] == "example-cache-test"

        # Second call should hit cache
        names2, usage2 = await company.get_names("example-cache-test.com")
        assert names1 == names2
        # Cache returns the exact same tuple
        # usage2 may be zeroed from cache or the same as usage1

    async def test_caching_different_domains_not_shared(self):
        """Different domains don't share cache entries."""
        names1, _ = await company.get_names("tesla.com")
        names2, _ = await company.get_names("ford.com")
        assert names1 != names2
        assert names1[0] == "tesla"
        assert names2[0] == "ford"

    async def test_case_insensitive(self):
        """Domain casing is normalized."""
        names1, _ = await company.get_names("NVIDIA.COM")
        names2, _ = await company.get_names("nvidia.com")
        assert names1 == names2
        assert names1[0] == "nvidia"

    async def test_returns_usage_dict(self):
        """get_names returns a proper usage dict."""
        names, usage = await company.get_names("amazon.com")
        assert isinstance(usage, dict)
        assert "input_tokens" in usage
        assert "output_tokens" in usage
        assert "total_tokens" in usage


class TestExtractStem:
    """Tests for the _extract_stem helper."""

    def test_basic(self):
        assert company._extract_stem("meta.com") == "meta"
        assert company._extract_stem("google.com") == "google"

    def test_www_prefix(self):
        assert company._extract_stem("www.meta.com") == "meta"

    def test_https(self):
        assert company._extract_stem("https://meta.com") == "meta"
        assert company._extract_stem("https://www.meta.com") == "meta"

    def test_with_path(self):
        assert company._extract_stem("meta.com/about") == "meta"

    def test_with_port(self):
        assert company._extract_stem("meta.com:8080") == "meta"

    def test_co_uk(self):
        assert company._extract_stem("bbc.co.uk") == "bbc"

    def test_subdomain(self):
        assert company._extract_stem("blog.meta.com") == "blog"


class TestParseNameList:
    """Tests for the _parse_name_list helper."""

    def test_json_array(self):
        text = '["meta", "meta platforms", "facebook"]'
        names = company._parse_name_list(text, "meta")
        assert names[0] == "meta"
        assert "meta platforms" in names
        assert "facebook" in names

    def test_with_markdown_code_block(self):
        text = '```json\n["google", "alphabet"]\n```'
        names = company._parse_name_list(text, "google")
        assert names[0] == "google"
        assert "alphabet" in names

    def test_stem_always_included(self):
        text = '["facebook", "fb"]'
        names = company._parse_name_list(text, "meta")
        assert names[0] == "meta"  # stem inserted at front
        assert "facebook" in names

    def test_deduplication(self):
        text = '["meta", "META", "meta", "facebook"]'
        names = company._parse_name_list(text, "meta")
        # Should dedupe case-insensitively
        assert names.count("meta") == 1

    def test_empty_array(self):
        text = '[]'
        names = company._parse_name_list(text, "meta")
        assert names == ["meta"]

    def test_invalid_json_fallback(self):
        text = 'not json at all'
        names = company._parse_name_list(text, "meta")
        assert names == ["meta"]

    def test_json_object_with_nested_array(self):
        text = '{"names": ["meta", "facebook"]}'
        names = company._parse_name_list(text, "meta")
        # The array inside the object is still extracted
        assert names[0] == "meta"
        assert "facebook" in names

    def test_extra_text_around_json(self):
        text = 'Here are the names: ["meta", "facebook"] hope that helps!'
        names = company._parse_name_list(text, "meta")
        assert names[0] == "meta"
        assert "facebook" in names
