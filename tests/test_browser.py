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
        {"action": "check", "checked": True},
        {"action": "check", "selector": "input"},
        {"action": "check", "selector": "input", "checked": "false"},
        {"action": "check", "selector": "input", "checked": 0},
        {"action": "check", "selector": "input", "checked": []},
    ],
)
def test_form_arguments_rejected_before_browser_start(tmp_path, arguments):
    with BrowserSession(tmp_path, ["example.com"]) as session:
        with pytest.raises(ValueError):
            session.operate(**arguments)
        assert session.page is None and session.driver is None


@pytest.mark.parametrize("checked", [True, False])
def test_check_accepts_boolean_target_and_requires_open_page(tmp_path, checked):
    with BrowserSession(tmp_path, ["example.com"]) as session:
        with pytest.raises(ValueError, match="Open a page first"):
            session.operate("check", selector="#consent", checked=checked)
        assert session.page is None and session.driver is None


@pytest.fixture
def checkbox_page(monkeypatch):
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
                body="""<title>Native control fixture</title>
                <label>Accept<input type="checkbox" id="accept"></label>
                <label>Basic<input type="radio" name="plan" value="basic"
                  id="basic" checked></label>
                <label>Plus<input type="radio" name="plan" value="plus" id="plus"></label>
                <label>Unavailable<input type="checkbox" id="PRIVATE_DISABLED" disabled></label>
                <div id="plain">Not a checkbox</div><p id="result"></p>
                <script>
                let changes = 0;
                function report() {
                  document.querySelector('#result').textContent =
                    'check=' + document.querySelector('#accept').checked + ';radio=' +
                    document.querySelector('input[name=plan]:checked').value +
                    ';changes=' + changes;
                }
                document.querySelectorAll('input').forEach(input => {
                  input.addEventListener('change', () => {changes++; report();});
                });
                report();
                </script>""",
            ),
        )

    monkeypatch.setattr(BrowserSession, "_start", start)


def test_chromium_check_sets_checkbox_and_radio_state_idempotently(tmp_path, checkbox_page):
    with BrowserSession(tmp_path, ["example.com"]) as session:
        tool = browser_tool(lambda: session)
        session.operate("open", url="https://example.com/")

        def check(selector, checked):
            raw = tool.invoke(
                json.dumps({"action": "check", "selector": selector, "checked": checked})
            )
            assert len(raw) <= 18000
            return json.loads(raw)

        first = check("#accept", True)
        assert "check=true;radio=basic;changes=1" in first["text"]
        assert 'checkbox "Accept" [checked]' in first["snapshot"]
        assert check("#accept", True) == first
        cleared = check("#accept", False)
        assert "check=false;radio=basic;changes=2" in cleared["text"]
        assert check("#accept", False) == cleared
        selected = check("#plus", True)
        assert "check=false;radio=plus;changes=3" in selected["text"]
        assert check("#plus", True) == selected
        assert "radio=basic;changes=4" in check("#basic", True)["text"]


@pytest.mark.parametrize(
    "selector,error", [("#PRIVATE_DISABLED", "TimeoutError"), ("#plain", "Error")]
)
def test_chromium_check_failure_has_private_source_evidence(
    store, project, tmp_path, checkbox_page, selector, error
):
    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                assert json.loads(messages[-1]["content"])["error"] == error
                return {"role": "assistant", "content": "Control could not be checked"}
            schema = next(
                t["function"]["parameters"] for t in tools if t["function"]["name"] == "browser"
            )
            assert "check" in schema["properties"]["action"]["enum"]
            assert schema["properties"]["checked"] == {"type": "boolean"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "check",
                        "type": "function",
                        "function": {
                            "name": "browser",
                            "arguments": json.dumps(
                                {"action": "check", "selector": selector, "checked": True}
                            ),
                        },
                    }
                ],
            }

    with BrowserSession(tmp_path, ["example.com"]) as session:
        session.operate("open", url="https://example.com/")
        session.executor.submit(session.context.set_default_timeout, 200).result()
        with AgentApp(
            Config(project=project, workspace=tmp_path), store=store, model=Model()
        ) as app:
            app.register(browser_tool(lambda: session))
            assert app.run("Synthetic checkbox task") == "Control could not be checked"
            assert app.last_run_metrics["tool_errors"] == 1
        assert "check=false;radio=basic;changes=0" in session.operate("snapshot")["text"]
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "tool_error"
    detail = json.loads(rows[0]["detail"])
    assert detail["capability"] == "browser" and detail["source_paths"] == [
        "src/evomesh/browser.py"
    ]
    assert selector not in rows[0]["detail"] and "Native control fixture" not in rows[0]["detail"]


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


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "15", None])
def test_browser_timeout_must_be_bounded(tmp_path, timeout):
    with pytest.raises(ValueError, match="finite positive"):
        BrowserSession(tmp_path, ["example.com"], timeout=timeout)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"selector": ""},
        {"selector": "#result", "state": ""},
        {"selector": "#result", "state": "ready"},
        {"selector": "#result", "state": []},
    ],
)
def test_wait_arguments_rejected_before_browser_start(tmp_path, arguments):
    with BrowserSession(tmp_path, ["example.com"]) as session:
        with pytest.raises(ValueError, match="selector|Wait state"):
            session.operate("wait", **arguments)
        assert session.page is None and session.driver is None
    with BrowserSession(tmp_path, ["example.com"]) as session:
        with pytest.raises(ValueError, match="Open a page first"):
            session.operate("wait", selector="#result")


@pytest.fixture
def async_browser_page(monkeypatch):
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
                body="""<title>Async fixture</title>
                <div id="result" hidden>Ready</div><div id="loading">Loading</div>
                <div id="pending">Pending</div><p id="status">Idle</p>
                <button onclick="setTimeout(() => {
                  document.querySelector('#result').hidden = false;
                  document.querySelector('#loading').hidden = true;
                  document.querySelector('#pending').remove();
                  const marker = document.createElement('div');
                  marker.id = 'marker'; marker.hidden = true;
                  document.body.appendChild(marker);
                  document.querySelector('#status').textContent = 'Completed';
                }, 200)">Start</button>""",
            ),
        )

    monkeypatch.setattr(BrowserSession, "_start", start)


@pytest.mark.parametrize(
    "selector,state",
    [("#result", None), ("#loading", "hidden"), ("#pending", "detached"), ("#marker", "attached")],
)
def test_chromium_wait_for_async_page(tmp_path, async_browser_page, selector, state):
    with BrowserSession(tmp_path, ["example.com"]) as session:
        tool = browser_tool(lambda: session)
        session.operate("open", url="https://example.com/")
        session.operate("click", selector="button")
        args = {"action": "wait", "selector": selector}
        if state is not None:
            args["state"] = state
        result = json.loads(tool.invoke(json.dumps(args)))
        assert result["title"] == "Async fixture"
        assert "Ready" in result["text"] and "Completed" in result["snapshot"]
        assert "Loading" not in result["text"] and "Pending" not in result["text"]
        # Already satisfied conditions return a fresh snapshot too.
        assert session.operate("wait", selector=selector, state=state) == result


def test_chromium_wait_timeout_evolution_evidence(store, project, tmp_path, async_browser_page):
    from playwright.sync_api import TimeoutError as BrowserTimeoutError

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                result = json.loads(messages[-1]["content"])
                assert result["error"] == "TimeoutError"
                assert "PRIVATE" not in messages[-1]["content"]
                return {"role": "assistant", "content": "Wait timed out"}
            schema = next(t for t in tools if t["function"]["name"] == "browser")
            assert "wait" in schema["function"]["parameters"]["properties"]["action"]["enum"]
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "wait",
                        "type": "function",
                        "function": {
                            "name": "browser",
                            "arguments": json.dumps(
                                {"action": "wait", "selector": "#PRIVATE_MISSING"}
                            ),
                        },
                    }
                ],
            }

    with BrowserSession(tmp_path, ["example.com"]) as session:
        session.operate("open", url="https://example.com/")
        # Keep browser launch independent of the deliberately short operation timeout.
        session.executor.submit(session.context.set_default_timeout, 200).result()
        with pytest.raises(BrowserTimeoutError):
            session.operate("wait", selector="#result")
        with AgentApp(
            Config(project=project, workspace=tmp_path), store=store, model=Model()
        ) as app:
            app.register(browser_tool(lambda: session))
            assert app.run("Synthetic async page") == "Wait timed out"
            assert app.last_run_metrics["tool_errors"] == 1
        assert session.operate("snapshot")["title"] == "Async fixture"
    row = store.observations(project)[0]
    detail = json.loads(row["detail"])
    assert row["kind"] == "tool_error" and detail["capability"] == "browser"
    assert detail["source_paths"] == ["src/evomesh/browser.py"]
    assert "PRIVATE" not in row["detail"] and "Async fixture" not in row["detail"]


@pytest.mark.parametrize("content", ["small", "x" * 20000, '\x01\\"' * 8000, "中文🙂" * 8000])
def test_browser_snapshot_budget_includes_escaping_metadata_and_screenshot(content):
    from evomesh.browser import _bounded_snapshot

    original = {field: content for field in ("url", "title", "snapshot", "text")}
    original["screenshot"] = "browser-artifacts/" + "a" * 32 + ".png"
    result = _bounded_snapshot(original)
    assert len(json.dumps(result, ensure_ascii=False)) <= 18000
    assert result["screenshot"] == original["screenshot"]
    assert result["original_chars"] == {
        field: len(content) for field in ("url", "title", "snapshot", "text")
    }
    clipped = [field for field in result["original_chars"] if result[field] != content]
    assert result["truncated_fields"] == clipped and result["truncated"] is bool(clipped)
    assert all(content.startswith(result[field]) for field in result["original_chars"])
    assert original["text"] == content  # The receipt builder must not modify the source.
    if content == "small":
        assert not result["truncated"]


def test_chromium_scoped_snapshot_recovers_large_page_and_records_private_evidence(
    tmp_path, store, project, monkeypatch
):
    if os.getenv("EVOMESH_TEST_BROWSER") != "1":
        pytest.skip("Enable EVOMESH_TEST_BROWSER after playwright install chromium")
    from html import escape

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("93.184.216.34", 443))]
    )
    original = BrowserSession._start
    large = '\\"' * 18000

    def start(self):
        original(self)
        self.context.route(
            "https://example.com/**",
            lambda route: route.fulfill(
                content_type="text/html",
                body="<title>Snapshot fixture</title><pre>"
                + escape(large)
                + '</pre><main id="result">PRIVATE_TARGET</main>',
            ),
        )

    monkeypatch.setattr(BrowserSession, "_start", start)

    class Model:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools):
            self.step += 1
            if self.step > 1:
                raw = messages[-1]["content"]
                assert len(raw) <= 18000
                page = json.loads(raw)
                assert page["title"] == "Snapshot fixture"
                if self.step == 2:
                    assert page["truncated"] and "text" in page["truncated_fields"]
                    assert page["original_chars"]["text"] > 36000
                    assert "PRIVATE_TARGET" not in page["text"]
                else:
                    assert not page["truncated"] and page["truncated_fields"] == []
                    assert page["text"] == "PRIVATE_TARGET"
                    assert "PRIVATE_TARGET" in page["snapshot"]
                    return {"role": "assistant", "content": "Read the target element"}
            args = (
                {"action": "open", "url": "https://example.com/"}
                if self.step == 1
                else {"action": "snapshot", "selector": "#result"}
            )
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "browser",
                        "type": "function",
                        "function": {"name": "browser", "arguments": json.dumps(args)},
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
        assert app.run("Synthetic large page") == "Read the target element"
        assert app.last_run_metrics["tool_errors"] == 0
        assert app.last_run_metrics["tool_output_truncations"] == 1
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "tool_output_limit"
    detail = json.loads(rows[0]["detail"])
    assert detail["capability"] == "browser" and detail["tool_truncated"]
    assert detail["source_paths"] == ["src/evomesh/browser.py"]
    assert detail["output_chars"] <= 18000
    assert "PRIVATE_TARGET" not in rows[0]["detail"] and "example.com" not in rows[0]["detail"]
