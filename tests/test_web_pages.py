import hashlib
import json
import socket

import httpx
import pytest

from evomesh import Agent
from evomesh.capabilities import WebPageChanged, web_tool


@pytest.fixture
def web_response(monkeypatch):
    response = {"text": "", "media_type": "text/plain"}
    real_client = httpx.Client
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host == "example.com":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
            ]
        # MySQL fixtures and connection pools still need their real resolver.
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": response["media_type"]}, text=response["text"]
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    return response


def test_web_fixture_keeps_database_address_resolution(web_response):
    addresses = socket.getaddrinfo("127.0.0.1", 3306, type=socket.SOCK_STREAM)
    assert addresses and all(item[0] == socket.AF_INET for item in addresses)
    assert all(item[4] == ("127.0.0.1", 3306) for item in addresses)


@pytest.mark.parametrize(
    "content,media_type,expected",
    [
        (
            "<h1>Hi</h1><script>hidden</script><p>中文🙂</p>",
            "Text/HTML; charset=utf-8",
            "Hi\n中文🙂",
        ),
        ("  <tag>literal</tag>\n&copy;\t", "text/plain", "  <tag>literal</tag>\n&copy;\t"),
        (
            '{"value":"<tag>literal</tag>&copy;"}',
            "application/json",
            '{"value":"<tag>literal</tag>&copy;"}',
        ),
    ],
)
def test_web_preserves_non_html_and_pages_extracted_characters(
    web_response, content, media_type, expected
):
    web_response.update(text=content, media_type=media_type)
    reader = web_tool(["example.com"])
    arguments = {"url": "https://example.com/page", "limit": 5}
    pieces = []
    while True:
        page = json.loads(reader.invoke(json.dumps(arguments)))
        pieces.append(page["text"])
        assert page["total_chars"] == len(expected)
        assert page["sha256"] == hashlib.sha256(expected.encode()).hexdigest()
        if page["next_offset"] is None:
            break
        arguments.update(offset=page["next_offset"], expected_sha256=page["sha256"])
    assert "".join(pieces) == expected


def test_web_long_pages_survive_agent_serialization_without_data_loss(web_response, store, project):
    content = 'PRIVATE_BODY 中文🙂\x01\t\\"' * 1500
    web_response["text"] = content
    received = []

    class Model:
        def complete(self, messages, tools):
            arguments = {"url": "https://example.com/PRIVATE_URL"}
            if messages[-1]["role"] == "tool":
                raw = messages[-1]["content"]
                assert len(raw) <= 18000
                page = json.loads(raw)
                assert page["offset"] == len("".join(received))
                received.append(page["text"])
                if page["next_offset"] is None:
                    return {"role": "assistant", "content": "Read every character"}
                arguments.update(offset=page["next_offset"], expected_sha256=page["sha256"])
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(len(received)),
                        "type": "function",
                        "function": {
                            "name": "read_web",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }

    agent = Agent(Model(), store, [web_tool(["example.com"])], project=project)
    assert agent.run("Read a synthetic page") == "Read every character"
    assert "".join(received) == content
    assert agent.metrics["tool_errors"] == agent.metrics["tool_output_truncations"] == 0
    assert not store.observations(project)


def test_web_change_is_recoverable_source_linked_evidence(web_response, store, project):
    reader = web_tool(["example.com"])
    web_response["text"] = "PRIVATE_OLD" * 10
    first = json.loads(reader.invoke('{"url":"https://example.com/PRIVATE_URL","limit":5}'))
    web_response["text"] = "PRIVATE_NEW" * 10
    arguments = {
        "url": "https://example.com/PRIVATE_URL",
        "offset": first["next_offset"],
        "expected_sha256": first["sha256"],
    }
    with pytest.raises(WebPageChanged):
        reader.invoke(json.dumps(arguments))

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                feedback = json.loads(messages[-1]["content"])
                assert feedback["error"] == "WebPageChanged"
                assert "Restart at offset zero" in feedback["feedback"]
                return {"role": "assistant", "content": "Must restart reading"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "read",
                        "type": "function",
                        "function": {
                            "name": "read_web",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }

    agent = Agent(Model(), store, [reader], project=project)
    assert agent.run("Read next page") == "Must restart reading"
    assert agent.metrics["tool_errors"] == 1
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "tool_error"
    detail = json.loads(rows[0]["detail"])
    assert detail["capability"] == "web"
    assert detail["source_paths"] == ["src/evomesh/capabilities.py", "src/evomesh/tools.py"]
    assert "PRIVATE_" not in json.dumps(rows)
    restarted = json.loads(reader.invoke('{"url":"https://example.com/PRIVATE_URL"}'))
    assert restarted["text"] == web_response["text"]
    assert restarted["sha256"] != first["sha256"]


def test_web_page_bounds_empty_content_and_response_limits(web_response):
    reader = web_tool(["example.com"])
    page = json.loads(reader.invoke('{"url":"https://example.com"}'))
    assert page["text"] == "" and page["next_offset"] is None and page["total_chars"] == 0
    with pytest.raises(ValueError, match="Offset"):
        reader.invoke('{"url":"https://example.com","offset":1}')
    web_response["text"] = "x" * 1_000_001
    with pytest.raises(ValueError, match="1 MB"):
        reader.invoke('{"url":"https://example.com"}')
    web_response.update(text="payload", media_type="application/json-untrusted")
    with pytest.raises(ValueError, match="Only text"):
        reader.invoke('{"url":"https://example.com"}')
