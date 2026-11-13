import json

import pytest

from evomesh import Agent, ToolFeedbackError, tool


@pytest.mark.parametrize(
    "message,truncated",
    [
        ("Use a known key", False),
        ("中文🙂" * 10000, True),
        ('\x01\t\\"' * 10000, True),
        ("\x01" * 5000, True),
    ],
)
def test_explicit_feedback_fits_output_and_keeps_shared_evidence_private(
    store, project, message, truncated
):
    feedback = "PRIVATE_FIXTURE " + message

    @tool(source_paths=("custom/lookup.py",), capability="lookup")
    def lookup(key: str) -> str:
        """Look up a fixture."""
        raise ToolFeedbackError(feedback)

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                raw = messages[-1]["content"]
                assert len(raw) <= 18000
                result = json.loads(raw)
                assert result["error"] == "ToolFeedbackError"
                assert result["feedback"].startswith("PRIVATE_FIXTURE ")
                assert feedback.startswith(result["feedback"])
                assert result["feedback_chars"] == len(feedback)
                assert result["feedback_truncated"] is (result["feedback"] != feedback)
                return {"role": "assistant", "content": "Inspect before correcting"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "lookup",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"key":"PRIVATE_ARGUMENT"}',
                        },
                    }
                ],
            }

    agent = Agent(Model(), store, [lookup], project=project)
    assert agent.run("Synthetic lookup") == "Inspect before correcting"
    assert agent.metrics["tool_errors"] == 1
    assert agent.metrics["tool_output_truncations"] == truncated
    for row in store.observations(project):
        assert "PRIVATE_" not in row["detail"]
        assert json.loads(row["detail"])["source_paths"] == ["custom/lookup.py"]
    assert "PRIVATE_" not in str(ToolFeedbackError(feedback))
    assert "PRIVATE_" not in repr(ToolFeedbackError(feedback))


def test_ordinary_exception_text_is_not_model_feedback(store, project):
    @tool
    def broken() -> str:
        """Raise a private internal error."""
        raise RuntimeError("PRIVATE_IMPLEMENTATION_DETAIL")

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                result = json.loads(messages[-1]["content"])
                assert result["error"] == "RuntimeError" and "feedback" not in result
                assert "PRIVATE_IMPLEMENTATION_DETAIL" not in json.dumps(messages)
                return {"role": "assistant", "content": "Unavailable"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "broken",
                        "type": "function",
                        "function": {
                            "name": "broken",
                            "arguments": "{}",
                        },
                    }
                ],
            }

    assert (
        Agent(Model(), store, [broken], project=project).run("Synthetic failure") == "Unavailable"
    )
    assert "PRIVATE_IMPLEMENTATION_DETAIL" not in json.dumps(store.observations(project))
