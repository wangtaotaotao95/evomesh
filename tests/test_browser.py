import json
import os
import socket

import pytest

from evomesh import AgentApp, Config
from evomesh.browser import BrowserSession, browser_tool, check_url


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "press", "key": "Enter"},
        {"action": "press", "selector": "input"},
        {"action": "press", "selector": "input", "key": ""},
        {"action": "select", "labels": ["Blue"]},
        {"action": "select", "selector": "select"},
        {"action": "select", "selector": "select", "values": [], "labels": []},
        {"action": "select", "selector": "select", "values": "blue"},
        {"action": "select", "selector": "select", "labels": [1]},
        {"action": "select", "selector": "select", "values": ["blue"] * 101},
    ],
)
def test_form_arguments_rejected_before_browser_start(tmp_path, arguments):
    with BrowserSession(tmp_path, ["example.com"]) as session:
        with pytest.raises(ValueError):
            session.operate(**arguments)
        assert session.page is None and session.driver is None


def test_form_tool_schema_and_private_failure_evidence(store, project, tmp_path):
    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                assert json.loads(messages[-1]["content"])["error"] == "ValueError"
                return {"role": "assistant", "content": "Correct the option arguments"}
            schema = next(
                t["function"]["parameters"] for t in tools if t["function"]["name"] == "browser"
            )
            assert {"press", "select"} <= set(schema["properties"]["action"]["enum"])
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "form",
                        "type": "function",
                        "function": {
                            "name": "browser",
                            "arguments": json.dumps(
                                {
                                    "action": "select",
                                    "selector": "#private-selector",
                                    "values": ["PRIVATE_OPTION"],
                                    "labels": ["PRIVATE_LABEL"],
                                }
                            ),
                        },
                    }
                ],
            }

    with AgentApp(
        Config(
            project=project, workspace=tmp_path, browser=True, browser_allowed_hosts=["example.com"]
        ),
        store=store,
        model=Model(),
    ) as app:
        assert app.run("Synthetic form") == "Correct the option arguments"
        assert app.last_run_metrics["tool_errors"] == 1
    row = store.observations(project)[0]
    detail = json.loads(row["detail"])
    assert row["kind"] == "tool_error" and detail["capability"] == "browser"
    assert detail["source_paths"] == ["src/evomesh/browser.py"]
    assert "private-selector" not in row["detail"] and "PRIVATE_" not in row["detail"]


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


def test_chromium_select_by_value_label_clear_and_keyboard_submit(tmp_path, monkeypatch):
    if os.getenv("EVOMESH_TEST_BROWSER") != "1":
        pytest.skip("Enable EVOMESH_TEST_BROWSER after playwright install chromium")
    from html import escape
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("93.184.216.34", 443))]
    )
    original = BrowserSession._start

    def fulfill(route):
        parsed = urlparse(route.request.url)
        if parsed.path == "/submitted":
            params = parse_qs(parsed.query)
            body = "<title>Submitted</title><p>" + escape(json.dumps(params)) + "</p>"
        else:
            body = """<title>Form fixture</title>
            <form action="/submitted">
              <label>Language<select id="language" name="language">
                <option value="en">English</option><option value="zh">中文</option>
              </select></label>
              <label>Colors<select id="colors" name="colors" multiple
                onchange="document.querySelector('#chosen').textContent=
                  'colors=' + Array.from(this.selectedOptions).map(o => o.value).join(',')">
                <option value="red">Red</option><option value="blue">Blue</option>
              </select></label>
              <label>Name<input id="name" name="name"></label>
              <button id="submit" onfocus="document.querySelector('#focus').textContent=
                'submit-focused'">Submit</button>
            </form><p id="chosen">unchanged</p><p id="focus">not-focused</p>"""
        route.fulfill(content_type="text/html; charset=utf-8", body=body)

    def start(self):
        original(self)
        self.context.route("https://example.com/**", fulfill)

    monkeypatch.setattr(BrowserSession, "_start", start)
    with BrowserSession(tmp_path, ["example.com"]) as session:
        tool = browser_tool(lambda: session)

        def invoke(action, **args):
            return json.loads(tool.invoke(json.dumps({"action": action, **args})))

        assert invoke("open", url="https://example.com/")["title"] == "Form fixture"
        invoke("select", selector="#language", labels=["中文"])
        assert (
            "colors=red,blue"
            in invoke("select", selector="#colors", values=["red", "blue"])["text"]
        )
        assert "colors=\n" in invoke("select", selector="#colors", values=[])["text"]
        assert "colors=blue" in invoke("select", selector="#colors", labels=["Blue"])["text"]
        invoke("fill", selector="#name", text="EvoMeshx")
        invoke("press", selector="#name", key="Backspace")
        assert "submit-focused" in invoke("press", selector="#name", key="Tab")["text"]
        submitted = invoke("press", selector="#name", key="Enter")
        assert submitted["title"] == "Submitted"
        assert json.loads(submitted["text"]) == {
            "language": ["zh"],
            "colors": ["blue"],
            "name": ["EvoMesh"],
        }
