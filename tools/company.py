"""Extract company name variations from a domain using LLM. Cached 24h."""

from cache import AsyncTTLCache
from providers import llm

# Module-level cache instance (24h TTL for company names)
_company_cache = AsyncTTLCache(ttl_seconds=86400)

_PROMPT_TEMPLATE = """Given the company domain '{domain}', extract all common name variations used to refer to this company in news articles and public discourse.

Return ONLY a JSON array of strings. Examples:
- "google.com" → ["google", "alphabet"]
- "meta.com" → ["meta", "meta platforms", "facebook", "fb"]
- "microsoft.com" → ["microsoft", "ms"]

Domain: {domain}
"""


def _extract_stem(domain: str) -> str:
    """Extract the domain stem (e.g., 'meta' from 'meta.com')."""
    domain = domain.strip().lower()
    # Remove protocol if present
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    # Remove path
    if "/" in domain:
        domain = domain.split("/", 0)[0]
    # Remove port
    if ":" in domain:
        domain = domain.split(":", 0)[0]
    # Remove www.
    if domain.startswith("www."):
        domain = domain[4:]
    # Take everything before the first dot (handles subdomains and multi-part TLDs)
    if "." in domain:
        domain = domain.split(".", 1)[0]
    return domain


async def get_names(company_domain: str) -> tuple[list[str], dict]:
    """Get company name variations from a domain.

    Args:
        company_domain: Company domain (e.g., "meta.com")

    Returns:
        (names_list, usage_dict) where names_list always includes the domain stem.
        Cached for 24 hours.
    """
    domain = company_domain.strip().lower()
    stem = _extract_stem(domain)

    async def _fetch() -> tuple[list[str], dict]:
        try:
            model = "deepseek/deepseek-v4-flash:nitro"
            prompt = _PROMPT_TEMPLATE.format(domain=domain)
            text, usage = await llm.call(model=model, max_tokens=256, prompt=prompt)

            # Parse JSON array from response
            names = _parse_name_list(text, stem)
            return names, usage
        except Exception:
            # Fallback to stem-only on any LLM failure
            return [stem], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    # Use cache key based on stem (different TLDs with same stem share cache)
    cache_key = f"company_names:{stem}"
    result = await _company_cache.get_or_compute(cache_key, _fetch)
    return result


def _parse_name_list(text: str, stem: str) -> list[str]:
    """Parse JSON array of names from LLM response, ensuring stem is included."""
    import json

    text = text.strip()

    # Try to extract JSON array from the text
    # The LLM might wrap it in markdown code blocks
    if "```" in text:
        # Extract content between code fences
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("["):
                text = part
                break

    # Try to find a JSON array in the text
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        names = json.loads(text)
        if isinstance(names, list):
            # Normalize: lowercase, strip, dedupe, preserve order
            seen = set()
            normalized = []
            for name in names:
                if isinstance(name, str):
                    n = name.strip().lower()
                    if n and n not in seen:
                        seen.add(n)
                        normalized.append(n)
            # Ensure stem is first
            if stem not in seen:
                normalized.insert(0, stem)
            else:
                # Move stem to front if present
                normalized.remove(stem)
                normalized.insert(0, stem)
            return normalized
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback to stem-only if parsing fails
    return [stem]
