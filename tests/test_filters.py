"""Filter tests — pure logic, no infrastructure. Runs in milliseconds."""

from datetime import datetime, timedelta

import pytest

from filters.dedup import dedup_urls
from filters.ranker import rank
from filters.stop_protocol import EXCLUDED_DOMAINS, apply_stop_protocol

# Current date for tests
_TODAY = datetime.now()
_TODAY_STR = _TODAY.strftime("%Y-%m-%d")
_30_DAYS_AGO = (_TODAY - timedelta(days=30)).strftime("%Y-%m-%d")
_60_DAYS_AGO = (_TODAY - timedelta(days=60)).strftime("%Y-%m-%d")


# ───────────────────────────────
# dedup.py
# ───────────────────────────────

class TestDedup:
    def test_empty_list(self):
        assert dedup_urls([]) == []

    def test_no_duplicates(self):
        results = [
            {"url": "https://a.com/1", "title": "A"},
            {"url": "https://b.com/2", "title": "B"},
        ]
        assert dedup_urls(results) == results

    def test_removes_duplicate_urls(self):
        results = [
            {"url": "https://a.com/1", "title": "First"},
            {"url": "https://a.com/1", "title": "Duplicate"},
            {"url": "https://b.com/2", "title": "B"},
        ]
        filtered = dedup_urls(results)
        assert len(filtered) == 2
        assert filtered[0]["title"] == "First"
        assert filtered[1]["title"] == "B"

    def test_preserves_first_occurrence_order(self):
        results = [
            {"url": "https://c.com", "title": "C"},
            {"url": "https://a.com", "title": "A"},
            {"url": "https://c.com", "title": "C2"},
            {"url": "https://b.com", "title": "B"},
        ]
        filtered = dedup_urls(results)
        assert [r["title"] for r in filtered] == ["C", "A", "B"]

    def test_skips_empty_urls(self):
        results = [
            {"url": "", "title": "No URL"},
            {"url": "https://a.com", "title": "A"},
        ]
        filtered = dedup_urls(results)
        assert len(filtered) == 1
        assert filtered[0]["title"] == "A"

    def test_missing_url_key(self):
        results = [
            {"title": "No URL key"},
            {"url": "https://a.com", "title": "A"},
        ]
        filtered = dedup_urls(results)
        assert len(filtered) == 1


# ───────────────────────────────
# stop_protocol.py
# ───────────────────────────────

class TestStopProtocol:
    def test_all_pass(self):
        """Result that passes all stages."""
        results = [
            {
                "title": "Meta Layoffs 2026",
                "url": "https://techcrunch.com/meta-layoffs",
                "content": "Meta announced layoffs today affecting 500 employees.",
                "published_date": _30_DAYS_AGO,
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=["meta", "facebook"],
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs", "jobs"],
        )
        assert len(filtered) == 1

    def test_excluded_domain_rejected(self):
        """Social media domains are rejected."""
        for domain in EXCLUDED_DOMAINS:
            results = [
                {
                    "title": "Post about Meta",
                    "url": f"https://{domain}/post",
                    "content": "Some content about meta layoffs",
                    "published_date": _30_DAYS_AGO,
                }
            ]
            filtered = apply_stop_protocol(
                results,
                topic="meta layoffs",
                company_names=["meta"],
                min_days=0,
                max_days=90,
                topic_keywords=["layoffs"],
            )
            assert len(filtered) == 0, f"{domain} should be excluded"

    def test_subdomain_excluded(self):
        """Subdomains of excluded domains are rejected."""
        results = [
            {
                "title": "Facebook Post",
                "url": "https://m.facebook.com/story",
                "content": "content about layoffs",
                "published_date": _30_DAYS_AGO,
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=["meta"],
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs"],
        )
        assert len(filtered) == 0

    def test_topic_relevance_no_match(self):
        """Results without topic keywords are rejected."""
        results = [
            {
                "title": "Weather today",
                "url": "https://example.com/weather",
                "content": "Sunny and warm",
                "published_date": _30_DAYS_AGO,
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=None,
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs", "meta"],
        )
        assert len(filtered) == 0

    def test_empty_keywords_rejects_all(self):
        """Empty keyword list rejects all results."""
        results = [
            {
                "title": "Anything",
                "url": "https://example.com",
                "content": "content",
                "published_date": _30_DAYS_AGO,
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="test",
            company_names=None,
            min_days=0,
            max_days=90,
            topic_keywords=[],
        )
        assert len(filtered) == 0

    def test_company_relevance_no_match(self):
        """Results without company names are rejected."""
        results = [
            {
                "title": "Google Layoffs",
                "url": "https://example.com/google",
                "content": "Google announced layoffs",
                "published_date": _30_DAYS_AGO,
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=["meta", "facebook"],
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs"],
        )
        assert len(filtered) == 0

    def test_company_relevance_skipped_when_none(self):
        """Company check is skipped when company_names is None."""
        results = [
            {
                "title": "Some Layoffs",
                "url": "https://example.com/news",
                "content": "Some company announced layoffs",
                "published_date": _30_DAYS_AGO,
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="layoffs",
            company_names=None,
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs"],
        )
        assert len(filtered) == 1

    def test_date_outside_window(self):
        """Results with dates outside the window are rejected."""
        old_date = (_TODAY - timedelta(days=200)).strftime("%Y-%m-%d")
        results = [
            {
                "title": "Old News",
                "url": "https://example.com/old",
                "content": "meta layoffs old news",
                "published_date": old_date,
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=["meta"],
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs"],
        )
        assert len(filtered) == 0

    def test_unknown_date_passes(self):
        """Results with unknown dates pass through."""
        results = [
            {
                "title": "Meta Layoffs",
                "url": "https://example.com/news",
                "content": "meta layoffs content",
                "published_date": "Unknown",
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=["meta"],
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs"],
        )
        assert len(filtered) == 1

    def test_missing_date_passes(self):
        """Results without published_date pass through."""
        results = [
            {
                "title": "Meta Layoffs",
                "url": "https://example.com/news",
                "content": "meta layoffs content",
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=["meta"],
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs"],
        )
        assert len(filtered) == 1

    def test_keyword_in_title(self):
        """Keyword match in title passes."""
        results = [
            {
                "title": "Meta Layoffs Announced",
                "url": "https://example.com/news",
                "content": "Brief summary",
                "published_date": _30_DAYS_AGO,
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=None,
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs"],
        )
        assert len(filtered) == 1

    def test_keyword_in_content(self):
        """Keyword match in content passes."""
        results = [
            {
                "title": "Tech News",
                "url": "https://example.com/news",
                "content": "Meta announced major layoffs today.",
                "published_date": _30_DAYS_AGO,
            }
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=None,
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs"],
        )
        assert len(filtered) == 1

    def test_multiple_results_mixed(self):
        """Multiple results with mixed outcomes."""
        results = [
            {
                "title": "Meta Layoffs",
                "url": "https://techcrunch.com/meta",
                "content": "Meta layoffs content",
                "published_date": _30_DAYS_AGO,
            },
            {
                "title": "Facebook Post",
                "url": "https://facebook.com/post",
                "content": "meta layoffs post",
                "published_date": _30_DAYS_AGO,
            },
            {
                "title": "Weather Report",
                "url": "https://example.com/weather",
                "content": "sunny day",
                "published_date": _30_DAYS_AGO,
            },
        ]
        filtered = apply_stop_protocol(
            results,
            topic="meta layoffs",
            company_names=["meta"],
            min_days=0,
            max_days=90,
            topic_keywords=["layoffs"],
        )
        assert len(filtered) == 1
        assert filtered[0]["title"] == "Meta Layoffs"


# ───────────────────────────────
# ranker.py
# ───────────────────────────────

class TestRanker:
    def test_empty_list(self):
        assert rank([], ["keyword"]) == []

    def test_single_result(self):
        results = [
            {"title": "Test", "url": "https://example.com", "content": "content", "published_date": _30_DAYS_AGO}
        ]
        assert rank(results, ["test"]) == results

    def test_recency_scoring(self):
        """More recent articles score higher."""
        today = _TODAY_STR
        old = (_TODAY - timedelta(days=60)).strftime("%Y-%m-%d")

        results = [
            {"title": "Old News", "url": "https://example.com/old", "content": "old", "published_date": old},
            {"title": "Today News", "url": "https://example.com/today", "content": "today", "published_date": today},
        ]
        ranked = rank(results, ["news"])
        assert ranked[0]["title"] == "Today News"
        assert ranked[1]["title"] == "Old News"

    def test_credible_domain_bonus(self):
        """Credible domains rank higher."""
        results = [
            {"title": "Unknown Source", "url": "https://random-blog.com/news", "content": "news", "published_date": _30_DAYS_AGO},
            {"title": "Reuters Report", "url": "https://reuters.com/news", "content": "news", "published_date": _30_DAYS_AGO},
        ]
        ranked = rank(results, ["news"])
        assert ranked[0]["title"] == "Reuters Report"
        assert ranked[1]["title"] == "Unknown Source"

    def test_keyword_in_title_scores_higher(self):
        """Keyword in title scores more than in content (per-keyword, not per-occurrence)."""
        results = [
            {
                "title": "General News",
                "url": "https://example.com",
                "content": "layoffs layoffs layoffs layoffs layoffs",  # still 1 keyword match = 5pts
                "published_date": _30_DAYS_AGO,
            },
            {
                "title": "Layoffs Announced",
                "url": "https://example.com",
                "content": "general content",  # 1 keyword match in title = 15pts
                "published_date": _30_DAYS_AGO,
            },
        ]
        ranked = rank(results, ["layoffs"])
        # Title match (15) > content match (5)
        assert ranked[0]["title"] == "Layoffs Announced"

    def test_headline_numbers_bonus(self):
        """Headlines with numbers get a bonus."""
        results = [
            {"title": "No Numbers Here", "url": "https://a.com", "content": "c", "published_date": _30_DAYS_AGO},
            {"title": "500 Layoffs", "url": "https://b.com", "content": "c", "published_date": _30_DAYS_AGO},
        ]
        ranked = rank(results, ["layoffs"])
        assert ranked[0]["title"] == "500 Layoffs"

    def test_content_length_bonus(self):
        """Longer content gets a bonus."""
        results = [
            {"title": "Short", "url": "https://a.com", "content": "x" * 10, "published_date": _30_DAYS_AGO},
            {"title": "Long", "url": "https://b.com", "content": "x" * 600, "published_date": _30_DAYS_AGO},
        ]
        ranked = rank(results, ["test"])
        assert ranked[0]["title"] == "Long"

    def test_stable_sort_equal_scores(self):
        """Equal scores preserve input order."""
        results = [
            {"title": "First", "url": "https://a.com", "content": "c", "published_date": _30_DAYS_AGO},
            {"title": "Second", "url": "https://b.com", "content": "c", "published_date": _30_DAYS_AGO},
        ]
        ranked = rank(results, ["none"])
        assert ranked[0]["title"] == "First"
        assert ranked[1]["title"] == "Second"

    def test_combined_scoring(self):
        """Multiple factors combine correctly."""
        today = _TODAY_STR

        results = [
            # Best: recent + credible + keyword in title + numbers + long content
            {
                "title": "Meta Fires 5000 Employees",
                "url": "https://reuters.com/meta",
                "content": "x" * 600,
                "published_date": today,
            },
            # Worst: old + unknown + no keywords + short
            {
                "title": "Old News",
                "url": "https://random.com",
                "content": "x" * 10,
                "published_date": (_TODAY - timedelta(days=100)).strftime("%Y-%m-%d"),
            },
            # Middle: recent + keyword in content
            {
                "title": "Tech Update",
                "url": "https://example.com",
                "content": "meta employees meta employees",
                "published_date": today,
            },
        ]
        ranked = rank(results, ["meta", "employees"])
        assert ranked[0]["title"] == "Meta Fires 5000 Employees"
        assert ranked[2]["title"] == "Old News"
