import json
import sys

import pytest

from evomesh import Agent, AgentApp, Config
from evomesh.capabilities import execute_code_tool, terminal_tool
from evomesh.tools import Tool, object_schema


def execution(tmp_path, kind, code):
    if kind == "terminal":
        return terminal_tool(tmp_path), {"command": [sys.executable, "-c", code]}
    return execute_code_tool(tmp_path), {"code": code}


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
@pytest.mark.parametrize(
    "prefix",
    ["", "short\n", "x" * 19990, "x" * 40000, '\x01\t\\"' * 4000, "中文🙂" * 5000],
    ids=["empty", "short", "metadata-overflow", "byte-overflow", "escaped", "unicode"],
)
def test_execution_preserves_structured_output_and_diagnostic_tail(tmp_path, kind, prefix):
    content = prefix + "FINAL_DIAGNOSTIC\n"
    code = f"import os; os.write(1, {content!r}.encode('utf-8')); raise SystemExit(7)"
    tool, arguments = execution(tmp_path, kind, code)
    serialized = tool.invoke(json.dumps(arguments))
    assert len(serialized) <= 18000
    result = json.loads(serialized)
    assert result["exit_code"] == 7
    assert result["output"].endswith("FINAL_DIAGNOSTIC\n")
    assert result["output_bytes"] == len(content.encode())
    assert result["truncated"] is (result["output"] != content)
    assert tool.result_is_error(serialized)
    assert tool.result_is_truncated(serialized) is result["truncated"]
    if len(content) < 100:
        assert result["output"] == content


class SingleCall:
    def __init__(self, name, arguments, verify):
        self.name, self.arguments, self.verify = name, arguments, verify

    def complete(self, messages, tools):
        if messages[-1]["role"] == "tool":
            self.verify(messages[-1]["content"])
            return {"role": "assistant", "content": "Done"}
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "execution",
                    "type": "function",
                    "function": {
                        "name": self.name,
                        "arguments": json.dumps(self.arguments),
                    },
                }
            ],
        }


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
@pytest.mark.parametrize("exit_code", [0, 2])
def test_execution_truncation_reaches_model_metrics_and_private_evidence(
    tmp_path, store, project, kind, exit_code
):
    tool, arguments = execution(
        tmp_path,
        kind,
        f"print(chr(1) * 18000 + 'PRIVATE_DIAGNOSTIC'); raise SystemExit({exit_code})",
    )

    def verify(raw):
        result = json.loads(raw)
        assert result["truncated"] is True
        assert result["exit_code"] == exit_code
        assert result["output"].endswith("PRIVATE_DIAGNOSTIC\n")

    with AgentApp(
        Config(project=project, workspace=tmp_path),
        store=store,
        tools=[tool],
        model=SingleCall(tool.name, arguments, verify),
    ) as app:
        assert app.run("Synthetic execution") == "Done"
        assert app.last_run_metrics["tool_output_truncations"] == 1
        assert app.last_run_metrics["tool_errors"] == (exit_code != 0)
        assert app.learning.list()[0]["metrics"]["tool_output_truncations"] == 1
    observations = store.observations(project)
    limits = [row for row in observations if row["kind"] == "tool_output_limit"]
    assert len(limits) == 1
    detail = json.loads(limits[0]["detail"])
    assert detail["tool_truncated"] is True
    assert detail["capability"] == ("terminal" if kind == "terminal" else "execution")
    assert detail["source_paths"] == ["src/evomesh/capabilities.py", "src/evomesh/processes.py"]
    assert detail["output_chars"] <= 18000
    assert all("PRIVATE_DIAGNOSTIC" not in row["detail"] for row in observations)


@pytest.mark.parametrize("reported", [False, True])
def test_custom_tool_outer_limit_records_one_observation(store, project, reported):
    tool = Tool(
        "long_output",
        "Synthetic long tool",
        object_schema({}),
        lambda: "x" * 21000,
        result_is_truncated=lambda result: reported,
    )

    def verify(raw):
        assert raw.startswith("x" * 20000)
        assert raw.endswith("[Tool output truncated at 20,000 characters]")

    agent = Agent(SingleCall(tool.name, {}, verify), store, [tool], project=project)
    assert agent.run("Synthetic tool") == "Done"
    assert agent.metrics["tool_output_truncations"] == 1
    assert agent.metrics["tool_errors"] == 0
    limits = [row for row in store.observations(project) if row["kind"] == "tool_output_limit"]
    assert len(limits) == 1
    detail = json.loads(limits[0]["detail"])
    assert detail["capability"] == "context"
    assert detail["tool_truncated"] is reported


def test_small_success_has_no_truncation_evidence(tmp_path, store, project):
    tool, arguments = execution(tmp_path, "execute_code", "print(42)")

    def verify(raw):
        assert json.loads(raw)["output"] == "42\n"

    agent = Agent(SingleCall(tool.name, arguments, verify), store, [tool], project=project)
    assert agent.run("Synthetic small execution") == "Done"
    assert agent.metrics["tool_output_truncations"] == 0
    assert agent.metrics["tool_errors"] == 0
    assert store.observations(project) == []
