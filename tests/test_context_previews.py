import json

import pytest

from evomesh import AgentApp, Config, tool
from evomesh.context import fit_context, request_size


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"text": "中文🙂" * 3000, "next_offset": 9000}, ensure_ascii=False),
        json.dumps([{"value": "x" * 1500}] * 5),
        '\x01\t\\"' * 1000,
        "plain output\n" * 1000,
    ],
)
def test_compaction_returns_valid_bounded_preview_not_broken_original_json(content):
    messages = [
        {"role": "system", "content": "System instructions"},
        {"role": "user", "content": "Current request"},
        {"role": "assistant", "tool_calls": [{"id": "result"}]},
        {"role": "tool", "tool_call_id": "result", "content": content},
    ]
    fitted, metrics = fit_context(messages, [], 1200)
    raw = fitted[-1]["content"]
    receipt = json.loads(raw)
    assert receipt["context_truncated"] is True
    assert receipt["original_chars"] == len(content)
    assert content.startswith(receipt["preview"])
    assert 0 < len(receipt["preview"]) < len(content)
    assert "not the original result" in receipt["notice"]
    assert "automatically repeat" in receipt["notice"]
    assert "next_offset" not in receipt
    assert len(json.dumps(raw, ensure_ascii=False)) <= 600
    assert request_size(fitted, []) <= 1200
    assert fitted[:-1] == messages[:-1]
    assert fitted[-1]["tool_call_id"] == "result"
    assert messages[-1]["content"] == content
    assert metrics["shortened_results"] == 1


def test_small_tool_result_is_not_wrapped():
    messages = [
        {"role": "user", "content": "Request"},
        {"role": "tool", "content": '{"ok":true}', "tool_call_id": "r"},
    ]
    fitted, metrics = fit_context(messages, [], 1200)
    assert fitted == messages and metrics["shortened_results"] == 0


def test_preview_never_expands_a_result_just_above_shortening_threshold():
    messages = [
        {"role": "user", "content": "Request"},
        {"role": "tool", "tool_call_id": "r", "content": "x" * 501},
    ]
    budget = request_size(messages, []) - 1
    fitted, metrics = fit_context(messages, [], budget)
    assert request_size(fitted, []) <= budget
    assert json.loads(fitted[-1]["content"])["context_truncated"]
    assert metrics["shortened_results"] == 1


def test_agent_keeps_full_private_receipt_and_records_only_context_metrics(
    store, project, tmp_path
):
    payload = {"text": "PRIVATE_SYNTHETIC_BODY" * 800, "exit_code": 0}
    executed = []

    @tool
    def inspect_fixture() -> dict:
        """Return a synthetic observation."""
        executed.append(True)
        return payload

    class Model:
        def complete(self, messages, tools):
            assert request_size(messages, tools) <= 16000
            if messages[-1]["role"] == "tool":
                receipt = json.loads(messages[-1]["content"])
                assert receipt["context_truncated"]
                assert "PRIVATE_SYNTHETIC_BODY" in receipt["preview"]
                assert "exit_code" not in receipt
                return {"role": "assistant", "content": "Only partial evidence available"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "inspect",
                        "type": "function",
                        "function": {
                            "name": "inspect_fixture",
                            "arguments": "{}",
                        },
                    }
                ],
            }

    with AgentApp(
        Config(project=project, workspace=tmp_path, context_chars=16000),
        store=store,
        model=Model(),
        tools=[inspect_fixture],
    ) as app:
        # The full built-in tool catalog needs a larger budget; use the lightweight
        # agent with this framework tool while retaining AgentApp session storage.
        app.tools = {"inspect_fixture": inspect_fixture}
        assert app.run("Inspect fixture", session="preview") == "Only partial evidence available"
        assert app.last_run_metrics["context_compactions"] == 1
        assert app.last_run_metrics["tool_errors"] == 0
        saved = app.state.session("preview")
        outputs = [m for m in saved if m["role"] == "tool"]
        assert json.loads(outputs[0]["content"]) == payload
    assert len(executed) == 1
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "context_pressure"
    assert json.loads(rows[0]["detail"])["capability"] == "context"
    assert "PRIVATE_" not in json.dumps(rows)
