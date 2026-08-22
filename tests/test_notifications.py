import pytest

from backend.push_server import PushModelArgs


def test_notification_subprocess_receives_only_its_provider_credentials(monkeypatch) -> None:
    import backend.mcp_servers as servers

    monkeypatch.setenv("PUSHOVER_USER", "test-user")
    monkeypatch.setenv("PUSHOVER_TOKEN", "test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")

    notification = servers.trader_mcp_servers()[0]

    assert notification.params.env["PUSHOVER_USER"] == "test-user"
    assert notification.params.env["PUSHOVER_TOKEN"] == "test-token"
    assert "OPENAI_API_KEY" not in notification.params.env


def test_disabled_notification_is_reported_as_an_error(monkeypatch) -> None:
    import backend.push_server as push_server

    monkeypatch.setattr(push_server, "pushover_user", None)
    monkeypatch.setattr(push_server, "pushover_token", None)

    with pytest.raises(RuntimeError, match="credentials are not configured"):
        push_server.push(PushModelArgs(message="Pending paper proposal"))


def test_notification_posts_to_pushover(monkeypatch) -> None:
    import backend.push_server as push_server

    monkeypatch.setattr(push_server, "pushover_user", "test-user")
    monkeypatch.setattr(push_server, "pushover_token", "test-token")
    observed = {}

    class Response:
        def raise_for_status(self) -> None:
            observed["status_checked"] = True

    def fake_post(url, *, data, timeout):
        observed.update(url=url, data=data, timeout=timeout)
        return Response()

    monkeypatch.setattr(push_server.requests, "post", fake_post)

    result = push_server.push(PushModelArgs(message="Pending paper proposal"))

    assert result == "Push notification sent"
    assert observed == {
        "url": "https://api.pushover.net/1/messages.json",
        "data": {
            "user": "test-user",
            "token": "test-token",
            "message": "Pending paper proposal",
        },
        "timeout": 10,
        "status_checked": True,
    }
