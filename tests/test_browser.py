import json
import os
import socket

import pytest

from evomesh import AgentApp, Config
from evomesh.browser import BrowserSession, check_url


def test_browser_url_policy(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("93.184.216.34", 443))]
    )
    check_url("https://example.com", ["example.com"])
    check_url("https://example.com", ["*"])
    for url in (
        "http://example.com",
        "file:///etc/passwd",
        "https://other.example",
        "https://user:pass@example.com",
        "https://example.com:8080",
    ):
        with pytest.raises(ValueError):
            check_url(url, ["example.com"])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="private"):
        check_url("https://example.com", ["*"])


def test_browser_lifetime_is_one_run_even_on_failure(store, project, tmp_path, monkeypatch):
    sessions = []

    class FakeSession:
        def __init__(self, *args):
            self.closed = False
            sessions.append(self)

        def operate(self, **kwargs):
            return {"text": "fixture"}

        def close(self):
            self.closed = True

    class Model:
        def complete(self, messages, tools):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "browser",
                        "type": "function",
                        "function": {
                            "name": "browser",
                            "arguments": json.dumps(
                                {"action": "open", "url": "https://example.com"}
                            ),
                        },
                    }
                ],
            }

    monkeypatch.setattr("evomesh.browser.BrowserSession", FakeSession)
    app = AgentApp(
        Config(
            project=project,
            workspace=tmp_path,
            browser=True,
            browser_allowed_hosts=["example.com"],
            max_steps=1,
        ),
        store=store,
        model=Model(),
    )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="step limit"):
            app.run("open fixture")
    assert len(sessions) == 2 and all(s.closed for s in sessions)


def test_chromium_open_fill_click_snapshot_screenshot(tmp_path, monkeypatch):
    if os.getenv("EVOMESH_TEST_BROWSER") != "1":
        pytest.skip("Enable EVOMESH_TEST_BROWSER after playwright install chromium")
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("93.184.216.34", 443))]
    )
    original = BrowserSession._start

    def start(self):
        original(self)
        self.context.route(
            "https://example.com/**",
            lambda route: route.fulfill(
                content_type="text/html",
                body="""<title>Fixture</title><label>Name<input id="name"></label>
            <button onclick="document.querySelector('p').textContent=
            document.querySelector('input').value">Greet</button>
            <p>Hello</p>""",
            ),
        )

    monkeypatch.setattr(BrowserSession, "_start", start)
    with BrowserSession(tmp_path, ["example.com"]) as session:
        assert session.operate("open", url="https://example.com/")["title"] == "Fixture"
        session.operate("fill", selector="#name", text="EvoMesh")
        assert "EvoMesh" in session.operate("click", selector="button")["text"]
        path = session.operate("screenshot")["screenshot"]
        assert (tmp_path / path).read_bytes().startswith(b"\x89PNG")
    with pytest.raises(ValueError, match="closed"):
        session.operate("snapshot")
