import json

import pytest

from backend import research_search_server as search_server


class Response:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == 8_192
        yield json.dumps(self.payload).encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise search_server.requests.HTTPError(str(self.status_code))

    def close(self):
        self.closed = True


def test_search_disables_raw_content_and_bounds_results(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "secret-test-key")
    observed = {}
    results = [
        {
            "url": f"https://example.com/news/{index}#tracking",
            "title": f"Story {index}",
            "content": f"Story {index} " + "x" * 1_000,
        }
        for index in range(8)
    ]

    def fake_post(url, **kwargs):
        observed.update(url=url, **kwargs)
        return Response({"results": results})

    monkeypatch.setattr(search_server.requests, "post", fake_post)
    bundle = search_server.bounded_search("material market news")

    assert len(bundle.results) == 5
    assert all(len(item.snippet) == 600 for item in bundle.results)
    assert all(item.publication_time_inferred for item in bundle.results)
    assert observed["url"] == search_server.API_URL
    assert observed["timeout"] == (5, 15)
    assert observed["stream"] is True
    assert observed["json"]["max_results"] == 5
    assert observed["json"]["include_raw_content"] is False
    assert observed["json"]["include_answer"] is False


def test_search_retries_rate_limit_with_bounded_delay(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "secret-test-key")
    responses = [Response({}, 429, {"Retry-After": "99"}), Response({"results": []})]
    delays = []
    monkeypatch.setattr(search_server.requests, "post", lambda *_args, **_kwargs: responses.pop(0))
    bundle = search_server.bounded_search("material market news", sleep=delays.append)
    assert bundle.results == []
    assert delays == [5]


def test_search_rejects_oversized_upstream_response(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "secret-test-key")

    class Oversized(Response):
        def iter_content(self, chunk_size):
            yield b"x" * (search_server.MAX_RESPONSE_BYTES + 1)

    monkeypatch.setattr(search_server.requests, "post", lambda *_args, **_kwargs: Oversized({}))
    with pytest.raises(ValueError, match="100KB"):
        search_server.bounded_search("material market news")
