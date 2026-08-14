"""Project-owned, size-bounded Tavily search MCP server."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from backend.research.models import canonicalize_url

API_URL = "https://api.tavily.com/search"
MAX_RESULTS = 5
MAX_SNIPPET_CHARACTERS = 600
MAX_RESPONSE_BYTES = 100_000
MAX_RETRIES = 2
TIMEOUT = (5, 15)

mcp = FastMCP("agentic-trading-floor-research-search")


class SearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoundedSearchResult(SearchModel):
    source_id: str
    canonical_url: str
    publisher: str
    title: str
    snippet: str
    retrieved_at: datetime
    published_at: datetime
    publication_time_inferred: bool


class BoundedSearchBundle(SearchModel):
    query: str
    retrieved_at: datetime
    results: list[BoundedSearchResult] = Field(max_length=MAX_RESULTS)


def _publication_time(value: object, retrieved_at: datetime) -> tuple[datetime, bool]:
    if not isinstance(value, str) or not value.strip():
        return retrieved_at, True
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return retrieved_at, True
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return (retrieved_at, True) if parsed > retrieved_at else (parsed, False)


def _response_payload(response) -> dict:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=8_192):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("Tavily response exceeded the 100KB safety limit")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Tavily returned an invalid JSON response") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Tavily returned an invalid response object")
    return payload


def bounded_search(query: str, *, sleep=time.sleep) -> BoundedSearchBundle:
    """Return at most five snippets; raw page content and generated answers are disabled."""
    normalized_query = " ".join(query.split())
    if not 3 <= len(normalized_query) <= 500:
        raise ValueError("search query must contain 3-500 characters")
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is required for bounded research search")
    response = None
    for attempt in range(MAX_RETRIES + 1):
        response = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "query": normalized_query,
                "topic": "news",
                "search_depth": "basic",
                "max_results": MAX_RESULTS,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
                "auto_parameters": False,
            },
            timeout=TIMEOUT,
            stream=True,
        )
        if response.status_code != 429 or attempt == MAX_RETRIES:
            break
        retry_after = response.headers.get("Retry-After", "1")
        response.close()
        try:
            delay = min(max(float(retry_after), 0), 5)
        except ValueError:
            delay = min(2**attempt, 5)
        sleep(delay)
    assert response is not None
    try:
        response.raise_for_status()
        payload = _response_payload(response)
    except requests.RequestException as exc:
        raise RuntimeError(f"Tavily search failed with HTTP {response.status_code}") from exc
    finally:
        response.close()

    retrieved_at = datetime.now(timezone.utc)
    results: list[BoundedSearchResult] = []
    seen_urls: set[str] = set()
    seen_snippets: set[str] = set()
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raw_results = []
    for item in raw_results:
        if len(results) >= MAX_RESULTS or not isinstance(item, dict):
            break
        try:
            url = canonicalize_url(str(item.get("url", "")))
        except (TypeError, ValueError):
            continue
        snippet = " ".join(str(item.get("content", "")).split())[:MAX_SNIPPET_CHARACTERS]
        title = " ".join(str(item.get("title", "")).split())[:500]
        if not snippet or not title or url in seen_urls or snippet in seen_snippets:
            continue
        published_at, inferred = _publication_time(
            item.get("published_date") or item.get("published_at"), retrieved_at
        )
        seen_urls.add(url)
        seen_snippets.add(snippet)
        results.append(
            BoundedSearchResult(
                source_id=f"search-{len(results) + 1}",
                canonical_url=url,
                publisher=(urlsplit(url).hostname or "unknown")[:200],
                title=title,
                snippet=snippet,
                retrieved_at=retrieved_at,
                published_at=published_at,
                publication_time_inferred=inferred,
            )
        )
    return BoundedSearchBundle(
        query=normalized_query,
        retrieved_at=retrieved_at,
        results=results,
    )


@mcp.tool()
def search(query: str) -> str:
    """Search recent financial news with at most five bounded snippets and no raw pages."""
    return bounded_search(query).model_dump_json()


if __name__ == "__main__":
    mcp.run(transport="stdio")
