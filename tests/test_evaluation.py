import json

import pytest

from evomesh import AgentApp, Config
from evomesh.evaluation import compare_reports, evaluate


def test_evaluation_assertions_budgets_and_regression(tmp_path):
    def factory():
        return AgentApp(
            Config(
                database_url="sqlite:///:memory:",
                demo=True,
                workspace=tmp_path / "workspace",
                repo=tmp_path,
            )
        )

    suite = [
        {
            "name": "list",
            "prompt": "List files",
            "contains": ["Workspace files"],
            "required_tools": ["list_files"],
            "forbidden_tools": ["write_file"],
            "max_model_calls": 2,
            "max_tool_calls": 1,
            "max_tool_errors": 0,
        }
    ]
    report = evaluate(factory, suite)
    assert report["passed"]
    assert "List files" not in json.dumps(report)
    bad = json.loads(json.dumps(report))
    bad["cases"][0]["passed"] = False
    assert compare_reports(report, bad)["regressions"] == ["list"]
    with pytest.raises(ValueError):
        compare_reports(report, {"suite_sha256": "other"})
    suite[0]["max_model_calls"] = 1
    failed = evaluate(factory, suite)
    assert not failed["passed"] and not failed["cases"][0]["checks"]["max_model_calls"]


def test_evaluation_requires_assertions_and_redacts_exceptions():
    with pytest.raises(ValueError):
        evaluate(None, [{"name": "empty", "prompt": "hello"}])

    def broken():
        raise RuntimeError("private-key-in-error")

    result = evaluate(
        broken, [{"name": "broken", "prompt": "private prompt", "contains": ["answer"]}]
    )
    assert not result["passed"] and result["cases"][0]["error"] == "RuntimeError"
    assert "private" not in json.dumps(result)
    with pytest.raises(ValueError):
        evaluate(broken, [{"name": "x", "prompt": "p", "max_model_calls": True}])
