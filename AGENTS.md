# Development Rules

## First Message

If the user did not give a concrete task, read these files in order:
1. `PRD.md` — what we're building and why
2. `docs/ARCHITECTURE.md` — system design, concurrency model, data flow
3. `docs/MODULES.md` — every module's interface and dependencies
4. `docs/RULES.md` — 19 mandatory constraints (violations are bugs)

Then ask which area to work on: providers, tools, filters, pipeline, api, or tests.

## Project Overview

Revgent v3 is an async-first corporate intelligence research agent. Three external providers (OpenRouter LLM, SearXNG search, trafilatura scrape), one event loop, zero threads for I/O. Built for high-concurrency Clay batch enrichment.

```
api.py              → FastAPI transport (thin, async def handlers)
core/pipeline.py    → One orchestration function: run(ctx, emit) → response
core/runner.py      → parallel(fn, items, max_workers) via asyncio.gather
tools/*             → Async functions composing providers with prompts
filters/*           → Pure sync functions, no I/O
providers/*         → Async adapters: llm.py, search.py, scrape.py
```

---

## Code Quality

### Python

- Python 3.11+. Use `|` union syntax, not `Union`. Use `list[str]`, not `List[str]`.
- Type hints on all public function signatures. Return types required.
- No `Any` unless the value genuinely has no known type. Prefer `dict[str, str]` over `dict[str, Any]` when the shape is known.
- No `# type: ignore` without an inline comment explaining why.
- f-strings for all string formatting. No `.format()`, no `%`.
- Frozen dataclasses for value objects (`@dataclass(frozen=True)`). Mutable dataclass only when mutation is the point.
- `__slots__` on classes instantiated per-request (`RunContext`, `TopicState`).
- No star imports (`from module import *`). Always import specific names.
- No circular imports. If two modules need each other, one of them is in the wrong layer.
- Imports ordered: stdlib, then third-party, then local. One blank line between groups.

### Async

- Every function that does I/O MUST be `async def`. Every I/O call MUST be `await`-ed.
- `time.sleep()` is forbidden. Use `asyncio.sleep()`.
- `requests` library is forbidden. Use `httpx.AsyncClient`.
- `anthropic.Anthropic` is forbidden. Use `anthropic.AsyncAnthropic`.
- `ThreadPoolExecutor` is forbidden. Use `asyncio.Semaphore` + `asyncio.gather`.
- The ONLY exception: `asyncio.run_in_executor(None, trafilatura.extract, ...)` for CPU-bound work.
- Pure functions (filters, formatting, ranking) stay sync. Do not make them async — async overhead on CPU-only work is waste.

### Error Handling

- Every function that can fail MUST either succeed or raise with actionable context.
- No bare `except Exception: return ""` or `except: pass`.
- Provider errors include: the URL that failed, the model that timed out, the HTTP status code.
- Scrape quality gate failure (trafilatura produces nothing useful) returns empty string — this is the ONE silent return, because it's not an error.
- Budget exhaustion is not an error. It produces a partial response.
- Missing env vars raise `ValueError` on first provider call with a message naming the variable.

### Naming

- Modules: lowercase, underscore-separated (`stop_protocol.py`, not `StopProtocol.py`)
- Functions: lowercase, underscore-separated (`validate_one`, not `validateOne`)
- Classes: PascalCase (`RunContext`, `ResearchDepthPolicy`)
- Constants: UPPER_SNAKE (`LLM_CONCURRENCY`, `EXCLUDED_DOMAINS`)
- Private helpers: single leading underscore (`_parse_date`, `_is_reasoning`)
- No abbreviations in public interfaces. `company_domain`, not `comp_dom`. `topic_keywords`, not `t_kws`.

---

## Architecture Rules

Read `docs/RULES.md` for the full 19 rules. The critical ones for daily work:

### Layer Discipline

```
api.py          → core/*           (NEVER import tools/* or providers/*)
core/pipeline   → tools/*, filters/*, core/runner, core/context
tools/*         → providers/*      (NEVER import core/pipeline or other tools)
filters/*       → nothing external (pure functions, no I/O, no provider calls)
providers/*     → external services only (NEVER import tools/* or core/*)
```

If you find yourself importing upward, the code is in the wrong layer.

### Provider / Tool / Pipeline Separation

- **Providers** own the connection, semaphore, and retry. They know nothing about topics or candidates.
- **Tools** compose providers with prompts. They do NOT own retry logic or parallelism.
- **Pipeline** orchestrates tools in sequence and calls `parallel()` for fan-out. It does NOT call providers directly.
- **Filters** are pure sync functions. No I/O. No awareness of pipeline state.

### Three Semaphores

Every external call goes through exactly one semaphore. Tool functions and the pipeline never touch semaphores — they call the provider's async function, which internally acquires the semaphore.

```
OpenRouter    Semaphore(24)   LLM_CONCURRENCY env var
SearXNG       Semaphore(12)   SEARCH_CONCURRENCY env var
Scrape        Semaphore(8)    SCRAPE_CONCURRENCY env var
```

### Error Isolation in Parallel Stages

`core/runner.parallel()` uses `asyncio.gather(return_exceptions=True)`. One failed candidate does NOT cancel the others. The pipeline MUST check `isinstance(result, BaseException)` for each item in the returned list.

---

## Commands

### After code changes

```bash
# Type check (get full output, no tail)
mypy revgent/ --strict

# Lint + format
ruff check revgent/ --fix
ruff format revgent/

# Fix all errors before committing.
```

### Running tests

```bash
# Full suite (includes real API calls — requires OPENROUTER_API_KEY + SearXNG running)
pytest

# Pure logic tests only (no API keys needed, sub-second)
pytest tests/ -m "not integration"

# Specific test file
pytest tests/test_stop_protocol.py -v

# Specific test
pytest tests/test_pipeline.py::test_cheap_depth -v
```

- NEVER run the API server during development: no `uvicorn api:app` or `python api.py`
- If you create or modify a test file, you MUST run it and iterate until it passes
- When writing tests, run them, identify issues in either test or implementation, and fix both

### Running locally (CLI)

```bash
# Single research run via CLI
python -m revgent.cli --company meta.com --topics layoffs --depth cheap
```

---

## Testing Rules

### No Mocks

No `unittest.mock`. No `monkeypatch`. No `MagicMock`. No module-level patching. No fake providers.

- Integration tests hit real OpenRouter, real SearXNG, real web pages
- Pure logic tests pass real data structures and assert on real return values
- Use `@pytest.mark.integration` for tests requiring infrastructure
- Use `@pytest.mark.asyncio` for all async tests

### What Makes a Good Test

- Calls the public interface with real inputs, asserts on real outputs
- Deterministic where possible (filters, formatting, ranking)
- Asserts on structural properties where LLM output varies (non-empty text, usage dict has expected keys, result count > 0)
- One behavior per test. Not "test_everything_works"
- Test name describes the behavior: `test_circuit_breaker_trips_after_two_failures`, not `test_search`

### Test Organization

```
tests/
├── test_providers/
│   ├── test_llm.py           # Real OpenRouter calls
│   ├── test_search.py        # Real SearXNG calls
│   └── test_scrape.py        # Real page fetches + SSRF
├── test_filters/
│   ├── test_stop_protocol.py # Pure logic
│   ├── test_dedup.py         # Pure logic
│   ├── test_ranker.py        # Pure logic
│   └── test_signals.py       # Pure logic
├── test_tools/
│   ├── test_validate.py      # Real LLM calls
│   └── test_format.py        # Real LLM calls
├── test_core/
│   ├── test_pipeline.py      # Full end-to-end
│   ├── test_runner.py        # Concurrency + ordering
│   └── test_context.py       # Pure logic
├── test_models.py            # UsageStats, CostTracker, cache
├── test_formatting.py        # parse_date, format_event
├── test_answer_builder.py    # build_answers
└── test_api.py               # FastAPI endpoint contracts
```

---

## Git Rules

### Committing

- NEVER commit unless the user asks
- ONLY commit files YOU changed in THIS session
- NEVER use `git add -A` or `git add .`
- ALWAYS use `git add <specific-file-paths>`
- Before committing, run `git status` and verify you are only staging your files
- Include `fixes #<number>` or `closes #<number>` when there's a related issue

### Commit Messages

Format: `<type>: <description>`

Types:
- `feat`: new functionality
- `fix`: bug fix
- `refactor`: restructuring without behavior change
- `test`: adding or modifying tests
- `docs`: documentation only
- `chore`: dependency updates, CI config

Examples:
```
feat: add circuit breaker to SearXNG provider
fix: handle empty trafilatura extraction without raising
refactor: extract company name resolution from stop_protocol
test: add SSRF rejection tests for private IP ranges
```

### Forbidden Git Operations

- `git reset --hard` — destroys uncommitted changes
- `git checkout .` — destroys uncommitted changes
- `git clean -fd` — deletes untracked files
- `git stash` — stashes ALL changes including other agents' work
- `git commit --no-verify` — bypasses checks, never allowed

---

## Style

- Keep answers short and concise
- No emojis in commits, code, or comments
- No fluff or cheerful filler text
- Technical prose only. Be direct.
- Code comments explain WHY, not WHAT. The code explains what.
- Docstrings on all public functions. One-line summary, then Args/Returns/Raises if non-obvious.

---

## Changelog

Location: `CHANGELOG.md` (single file, project root)

### Format

```markdown
## [Unreleased]

### Added
- New features

### Changed
- Changes to existing functionality

### Fixed
- Bug fixes

### Removed
- Removed features
```

### Rules

- New entries ALWAYS go under `## [Unreleased]`
- Append to existing subsections, do not create duplicates
- NEVER modify already-released version sections
- Read the full `[Unreleased]` section before adding entries to check for existing subsections

---

## Environment Setup

```bash
# Clone
git clone <repo-url>
cd internal-revgent-v3

# Install
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env: set OPENROUTER_API_KEY, optionally SEARXNG_URL

# Verify
pytest tests/test_filters/ -v  # should pass without API keys
```

### Required Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `OPENROUTER_API_KEY` | Yes | — | OpenRouter API authentication |
| `SEARXNG_URL` | No | `http://localhost:8888` | Self-hosted SearXNG instance URL |
| `LLM_CONCURRENCY` | No | `24` | Max concurrent OpenRouter calls |
| `SEARCH_CONCURRENCY` | No | `12` | Max concurrent SearXNG queries |
| `SCRAPE_CONCURRENCY` | No | `8` | Max concurrent page scrapes |
| `DEFAULT_MAX_COST` | No | per-depth | Override default budget ceiling |
| `ABSOLUTE_MAX_COST` | No | — | Hard ceiling on any request's max_cost |

---

## Dependencies

Only what's used. No libraries for features that don't exist.

```
anthropic>=0.40.0          # AsyncAnthropic for OpenRouter
httpx>=0.27.0              # Async HTTP (also anthropic transitive dep)
python-dotenv>=1.0.0       # .env loading
fastapi>=0.115.0           # API framework
uvicorn[standard]>=0.32.0  # ASGI server
trafilatura>=2.0.0         # HTML content extraction
pytest>=8.0.0              # Testing
pytest-asyncio>=0.24.0     # Async test support
```

**Explicitly excluded:** `tavily-python`, `firecrawl`, `requests`, `aiohttp`. If you find yourself reaching for one of these, you're solving the wrong problem.

---

## Key Invariants

These must be true at all times. If a change breaks any of them, the change is wrong.

1. **Budget enforcement:** a request with `max_cost=0.01` never spends more than ~$0.012 (budget checked between stages, not mid-stage)
2. **Cost attribution:** `sum(item.cost_attribution for item in events + signals) ≈ cost.total_cost`
3. **Response shape:** every field in the v2 ResearchResponse Pydantic model exists with the same type in v3
4. **Source order:** parallel tool execution returns results in input order
5. **SSRF protection:** no HTTP request to a private/loopback/link-local IP, even via redirect chain or DNS rebinding
6. **Error isolation:** one failed candidate in a parallel stage does not cancel the others
7. **No threads for I/O:** the only thread usage is `run_in_executor` for trafilatura CPU work
8. **Semaphore gating:** every external call goes through its provider's semaphore

---

## CRITICAL Rules

- NEVER use `sed` or `cat` to read files. Use the read tool (with offset + limit for ranged reads).
- You MUST read every file you modify in full before editing.
- NEVER add `requests`, `tavily-python`, `firecrawl`, or `aiohttp` to dependencies.
- NEVER use `time.sleep()`. Use `asyncio.sleep()`.
- NEVER use `ThreadPoolExecutor()`. Use `asyncio.Semaphore` + `asyncio.gather`.
- NEVER use `unittest.mock` or `monkeypatch` in tests.
- NEVER use `print()` for pipeline observability. Use the `emit` callback with typed events.
- NEVER use `asyncio.gather()` without `return_exceptions=True` in parallel stages.
- NEVER create a separate pipeline function for cheap/standard/deep modes. One function, conditional branches.
- NEVER call providers directly from the pipeline. Always go through tools.
- NEVER import upward in the layer hierarchy (tools cannot import pipeline, providers cannot import tools).

### User Override

If user instructions conflict with rules here, ask for confirmation that they want to override. Only then execute.
